"""Bounded MiniMax Anthropic-compatible client behind the ToolGateway."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from vaf.adapters.tool_gateway import ToolGateway
from vaf.policy.engine import PolicyEngine, ToolRequest
from vaf.ports.agents import ImageInput
from vaf.ports.tools import ToolExecutionOutput


DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/anthropic"
DEFAULT_MINIMAX_MODEL = "MiniMax-M3"
ALLOWED_MINIMAX_HOSTS = {"api.minimax.io", "api.minimaxi.com"}
MAX_PROVIDER_RESPONSE_BYTES = 20 * 1024 * 1024
ALLOWED_MULTIMODAL_MEDIA_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}


class MiniMaxProviderError(RuntimeError):
    """A safe provider error that never contains credentials."""


@dataclass(frozen=True)
class MiniMaxConfig:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_MINIMAX_BASE_URL
    model: str = DEFAULT_MINIMAX_MODEL
    timeout_seconds: int = 600
    artifact_max_tokens: int = 8192
    code_max_tokens: int = 32768

    @classmethod
    def from_env(cls) -> "MiniMaxConfig":
        api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
        if not api_key:
            raise MiniMaxProviderError("MINIMAX_API_KEY 未配置")
        config = cls(
            api_key=api_key,
            base_url=os.environ.get("MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL).strip(),
            model=os.environ.get("MINIMAX_MODEL", DEFAULT_MINIMAX_MODEL).strip(),
            timeout_seconds=_env_int("MINIMAX_TIMEOUT_SECONDS", 600),
            artifact_max_tokens=_env_int("MINIMAX_ARTIFACT_MAX_TOKENS", 8192),
            code_max_tokens=_env_int("MINIMAX_CODE_MAX_TOKENS", 32768),
        )
        config.validate()
        return config

    def validate(self) -> None:
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_MINIMAX_HOSTS
            or parsed.path.rstrip("/") != "/anthropic"
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise MiniMaxProviderError(
                "MINIMAX_BASE_URL 必须是 MiniMax 官方 Anthropic 端点"
            )
        if not self.model.startswith("MiniMax-"):
            raise MiniMaxProviderError("MINIMAX_MODEL 必须是 MiniMax 模型 ID")
        if not 10 <= self.timeout_seconds <= 1800:
            raise MiniMaxProviderError("MINIMAX_TIMEOUT_SECONDS 必须在 10 到 1800 之间")
        for name, value in (
            ("MINIMAX_ARTIFACT_MAX_TOKENS", self.artifact_max_tokens),
            ("MINIMAX_CODE_MAX_TOKENS", self.code_max_tokens),
        ):
            if not 256 <= value <= 204800:
                raise MiniMaxProviderError(f"{name} 必须在 256 到 204800 之间")

    def public_dict(self) -> dict[str, object]:
        parsed = urlparse(self.base_url)
        return {
            "provider": "minimax",
            "model": self.model,
            "endpoint": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "key_configured": bool(self.api_key),
            "multimodal": self.model == "MiniMax-M3",
        }


@dataclass(frozen=True)
class MiniMaxToolResult:
    value: dict[str, object]
    model: str
    request_id: str
    stop_reason: str
    usage: dict[str, object]
    output_hash: str
    input_image_hashes: tuple[str, ...] = ()

    def evidence(self) -> dict[str, object]:
        return {
            "provider": "minimax",
            "model": self.model,
            "request_id": self.request_id,
            "stop_reason": self.stop_reason,
            "usage": self.usage,
            "output_hash": self.output_hash,
            "multimodal": bool(self.input_image_hashes),
            "input_image_count": len(self.input_image_hashes),
            "input_image_hashes": list(self.input_image_hashes),
        }


Transport = Callable[[MiniMaxConfig, dict[str, object]], dict[str, object]]


class MiniMaxMessageAdapter:
    def __init__(self, config: MiniMaxConfig, transport: Transport | None = None) -> None:
        self.config = config
        self.transport = transport or _send_anthropic_message

    def execute(self, args: dict[str, object], workspace_root: Path) -> ToolExecutionOutput:
        del workspace_root
        image_args = args.get("images", [])
        if not isinstance(image_args, list):
            raise MiniMaxProviderError("MiniMax images 必须是列表")
        user_content, image_hashes = _build_user_content(str(args["prompt"]), image_args)
        payload = {
            "model": self.config.model,
            "max_tokens": int(args["max_tokens"]),
            "temperature": float(args.get("temperature", 0.2)),
            "system": str(args["system"]),
            "messages": [{"role": "user", "content": user_content}],
            "tools": [
                {
                    "name": str(args["tool_name"]),
                    "description": "Return the validated VAF result.",
                    "input_schema": args["input_schema"],
                }
            ],
            "tool_choice": {"type": "tool", "name": str(args["tool_name"])},
        }
        response = self.transport(self.config, payload)
        result = _parse_tool_response(response, str(args["tool_name"]), image_hashes=image_hashes)
        return ToolExecutionOutput(exit_code=0, stdout=json.dumps(result, ensure_ascii=False))


class MiniMaxClient:
    def __init__(self, config: MiniMaxConfig, transport: Transport | None = None) -> None:
        self.config = config
        adapter = MiniMaxMessageAdapter(config, transport)
        policy = PolicyEngine(allowed_network_modes={"llm-provider"})
        self.gateway = ToolGateway(policy, {"llm_complete": adapter})

    def invoke_tool(
        self,
        *,
        system: str,
        prompt: str,
        tool_name: str,
        input_schema: dict[str, object],
        max_tokens: int,
        workspace_root: Path,
        images: tuple[ImageInput, ...] = (),
    ) -> MiniMaxToolResult:
        image_args = [_serialize_image(image) for image in images]
        idempotency_source = json.dumps(
            {
                "system": system,
                "prompt": prompt,
                "tool_name": tool_name,
                "input_schema": input_schema,
                "max_tokens": max_tokens,
                "image_hashes": [image.content_hash for image in images],
                "model": self.config.model,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = self.gateway.execute(
            ToolRequest(
                tool_name="llm_complete",
                args={
                    "system": system,
                    "prompt": prompt,
                    "tool_name": tool_name,
                    "input_schema": input_schema,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "images": image_args,
                },
                workspace_root=workspace_root,
                network_mode="llm-provider",
                run_id="RUN-LLM-PROVIDER",
                change_id="CHG-LLM-PROVIDER",
                stage_run_id="agent-generation",
                idempotency_key=f"sha256:{sha256(idempotency_source.encode('utf-8')).hexdigest()}",
            )
        )
        if not result.output or result.output.exit_code != 0:
            detail = result.output.stderr if result.output else result.error_code
            raise MiniMaxProviderError(f"MiniMax 调用失败：{detail or 'unknown error'}")
        try:
            payload = json.loads(result.output.stdout)
            value = payload["value"]
            if not isinstance(value, dict):
                raise TypeError("tool value must be an object")
            return MiniMaxToolResult(
                value=value,
                model=str(payload["model"]),
                request_id=str(payload["request_id"]),
                stop_reason=str(payload["stop_reason"]),
                usage=dict(payload.get("usage") or {}),
                output_hash=str(payload["output_hash"]),
                input_image_hashes=tuple(str(value) for value in payload.get("input_image_hashes", [])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MiniMaxProviderError("MiniMax 返回了无效的结构化结果") from exc


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise MiniMaxProviderError("MiniMax 请求发生重定向，已阻止凭据转发")


def _send_anthropic_message(
    config: MiniMaxConfig,
    payload: dict[str, object],
) -> dict[str, object]:
    endpoint = f"{config.base_url.rstrip('/')}/v1/messages"
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "X-Api-Key": config.api_key,
            "Anthropic-Version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "VAF/0.1 MiniMaxProvider",
        },
        method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=config.timeout_seconds) as response:
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raw = exc.read(8192)
        raise MiniMaxProviderError(
            f"MiniMax HTTP {exc.code}：{_safe_provider_error(raw)}"
        ) from exc
    except URLError as exc:
        raise MiniMaxProviderError(f"MiniMax 网络错误：{str(exc.reason)[:300]}") from exc
    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise MiniMaxProviderError("MiniMax 响应超过 20 MB 限制")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MiniMaxProviderError("MiniMax 返回了非 JSON 响应") from exc
    if not isinstance(payload, dict):
        raise MiniMaxProviderError("MiniMax 返回结构不是对象")
    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict) and base_resp.get("status_code") not in {None, 0}:
        raise MiniMaxProviderError(
            f"MiniMax 服务错误 {base_resp.get('status_code')}：{str(base_resp.get('status_msg', ''))[:300]}"
        )
    return payload


def _parse_tool_response(
    response: dict[str, object],
    tool_name: str,
    *,
    image_hashes: tuple[str, ...] = (),
) -> dict[str, object]:
    blocks = response.get("content")
    if not isinstance(blocks, list):
        raise MiniMaxProviderError("MiniMax 响应缺少 content")
    tool_calls = [
        block
        for block in blocks
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == tool_name
    ]
    if len(tool_calls) != 1 or not isinstance(tool_calls[0].get("input"), dict):
        raise MiniMaxProviderError("MiniMax 未按要求返回唯一结构化 Tool 结果")
    value = tool_calls[0]["input"]
    normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "value": value,
        "model": str(response.get("model") or ""),
        "request_id": str(response.get("id") or ""),
        "stop_reason": str(response.get("stop_reason") or ""),
        "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
        "output_hash": f"sha256:{sha256(normalized.encode('utf-8')).hexdigest()}",
        "input_image_hashes": list(image_hashes),
    }


def _serialize_image(image: ImageInput) -> dict[str, object]:
    if not image.content_hash.startswith("sha256:"):
        raise MiniMaxProviderError("多模态图片缺少 SHA-256 内容哈希")
    if image.data is not None and image.url is not None:
        raise MiniMaxProviderError("多模态图片不能同时包含 data 和 url")
    if image.data is not None:
        if image.media_type not in ALLOWED_MULTIMODAL_MEDIA_TYPES:
            raise MiniMaxProviderError("多模态图片格式不受支持")
        return {
            "source_type": "base64",
            "media_type": image.media_type,
            "data": base64.b64encode(image.data).decode("ascii"),
            "content_hash": image.content_hash,
        }
    if image.url is not None:
        parsed = urlparse(image.url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise MiniMaxProviderError("多模态图片 URL 必须是无凭据的 HTTPS 地址")
        return {
            "source_type": "url",
            "url": image.url,
            "content_hash": image.content_hash,
        }
    raise MiniMaxProviderError("多模态图片没有可发送的内容")


def _build_user_content(
    prompt: str,
    images: list[object],
) -> tuple[str | list[dict[str, object]], tuple[str, ...]]:
    if not images:
        return prompt, ()
    blocks: list[dict[str, object]] = []
    hashes: list[str] = []
    for raw in images:
        if not isinstance(raw, dict):
            raise MiniMaxProviderError("MiniMax images 中存在无效项目")
        source_type = raw.get("source_type")
        content_hash = str(raw.get("content_hash") or "")
        if not content_hash.startswith("sha256:"):
            raise MiniMaxProviderError("多模态图片缺少 SHA-256 内容哈希")
        if source_type == "base64":
            media_type = str(raw.get("media_type") or "")
            data = str(raw.get("data") or "")
            if media_type not in ALLOWED_MULTIMODAL_MEDIA_TYPES or not data:
                raise MiniMaxProviderError("MiniMax Base64 图片格式无效")
            source = {"type": "base64", "media_type": media_type, "data": data}
        elif source_type == "url":
            url = str(raw.get("url") or "")
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise MiniMaxProviderError("MiniMax 图片 URL 必须是无凭据的 HTTPS 地址")
            source = {"type": "url", "url": url}
        else:
            raise MiniMaxProviderError("MiniMax 图片来源类型无效")
        blocks.append({"type": "image", "source": source})
        hashes.append(content_hash)
    blocks.append({"type": "text", "text": prompt})
    return blocks, tuple(hashes)


def _safe_provider_error(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")[:300]
    if not isinstance(payload, dict):
        return "invalid provider error"
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "provider error")[:300]
    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict):
        return str(base_resp.get("status_msg") or base_resp.get("status_code") or "provider error")[:300]
    return "provider error"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise MiniMaxProviderError(f"{name} 必须是整数") from exc
