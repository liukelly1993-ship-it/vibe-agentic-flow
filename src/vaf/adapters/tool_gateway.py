"""Single gateway for policy evaluation, execution, audit, and idempotency."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping

from vaf.domain.events import EventEnvelope
from vaf.domain.ids import new_id
from vaf.adapters.jsonl_event_store import JsonlEventStore
from vaf.policy.engine import PolicyDecision, PolicyDecisionType, PolicyEngine, ToolRequest
from vaf.ports.tools import ToolAdapter, ToolExecutionOutput


@dataclass(frozen=True)
class ToolExecutionResult:
    invocation_id: str
    idempotency_key: str
    decision: PolicyDecision
    executed: bool
    reused: bool = False
    output: ToolExecutionOutput | None = None
    error_code: str | None = None


class LocalCommandAdapter:
    """Run an allowlisted argv without invoking a shell."""

    def __init__(self, timeout_seconds: int = 300) -> None:
        self.timeout_seconds = timeout_seconds

    def execute(self, args: dict[str, object], workspace_root: Path) -> ToolExecutionOutput:
        argv = args.get("argv")
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ValueError("run_command requires a non-empty argv list")
        try:
            completed = subprocess.run(
                [str(item) for item in argv],
                cwd=workspace_root,
                env=_command_environment(workspace_root),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ToolExecutionOutput(
                exit_code=124,
                stdout=_redact(str(exc.stdout or "")),
                stderr="command timed out",
            )
        return ToolExecutionOutput(
            exit_code=completed.returncode,
            stdout=_redact(completed.stdout),
            stderr=_redact(completed.stderr),
        )


class LocalFileAdapter:
    """Write one declared file inside the already isolated workspace."""

    def execute(self, args: dict[str, object], workspace_root: Path) -> ToolExecutionOutput:
        path = args.get("path")
        content = args.get("content")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("write_file requires a non-empty path")
        if not isinstance(content, str):
            raise ValueError("write_file requires string content")
        target = Path(path) if Path(path).is_absolute() else workspace_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolExecutionOutput(exit_code=0, stdout=f"wrote {path}")


class ToolGateway:
    """The only supported entry point for external tool execution."""

    def __init__(
        self,
        policy: PolicyEngine,
        adapters: Mapping[str, ToolAdapter] | None = None,
        event_store: JsonlEventStore | None = None,
    ) -> None:
        self.policy = policy
        self.adapters = dict(adapters or {})
        self.event_store = event_store
        self._completed: dict[str, ToolExecutionResult] = {}

    def execute(self, request: ToolRequest) -> ToolExecutionResult:
        invocation_id = new_id("INV")
        idempotency_key = request.idempotency_key or self._default_key(request)
        cached = self._completed.get(idempotency_key)
        if cached is None and self.event_store is not None:
            cached = self._restore_completed(request, idempotency_key)
        if cached is not None:
            self._completed[idempotency_key] = cached
            return ToolExecutionResult(
                invocation_id=cached.invocation_id,
                idempotency_key=idempotency_key,
                decision=cached.decision,
                executed=cached.executed,
                reused=True,
                output=cached.output,
                error_code=cached.error_code,
            )

        self._emit(
            "ToolInvocationRequested",
            request,
            {"invocation_id": invocation_id, "idempotency_key": idempotency_key},
        )
        decision = self.policy.evaluate(request)
        self._emit(
            "PolicyDecisionMade",
            request,
            {
                "invocation_id": invocation_id,
                "decision_id": decision.decision_id,
                "decision": decision.decision.value,
                "rule_id": decision.rule_id,
                "reason": decision.reason,
            },
        )
        if decision.decision != PolicyDecisionType.ALLOW:
            return ToolExecutionResult(
                invocation_id=invocation_id,
                idempotency_key=idempotency_key,
                decision=decision,
                executed=False,
                error_code="VAF-POLICY-001",
            )

        adapter = self.adapters.get(request.tool_name)
        if adapter is None:
            result = ToolExecutionResult(
                invocation_id=invocation_id,
                idempotency_key=idempotency_key,
                decision=decision,
                executed=False,
                error_code="VAF-TOOL-UNSUPPORTED",
            )
            self._emit("ToolInvocationCompleted", request, {"invocation_id": invocation_id, "error_code": result.error_code})
            return result

        try:
            output = adapter.execute(request.args, request.workspace_root)
        except Exception as exc:  # adapter boundary converts failures into evidence
            result = ToolExecutionResult(
                invocation_id=invocation_id,
                idempotency_key=idempotency_key,
                decision=decision,
                executed=True,
                error_code="VAF-TOOL-001",
                output=ToolExecutionOutput(exit_code=1, stderr=_redact(str(exc))),
            )
        else:
            result = ToolExecutionResult(
                invocation_id=invocation_id,
                idempotency_key=idempotency_key,
                decision=decision,
                executed=True,
                output=output,
                error_code=None if output.exit_code == 0 else "VAF-TOOL-001",
            )
        self._completed[idempotency_key] = result
        self._emit(
            "ToolInvocationCompleted",
            request,
            {
                "invocation_id": invocation_id,
                "idempotency_key": idempotency_key,
                "executed": result.executed,
                "decision_id": result.decision.decision_id,
                "decision": result.decision.decision.value,
                "rule_id": result.decision.rule_id,
                "reason": result.decision.reason,
                "exit_code": result.output.exit_code if result.output else None,
                "error_code": result.error_code,
                "stdout": _truncate(result.output.stdout if result.output else ""),
                "stderr": _truncate(result.output.stderr if result.output else ""),
            },
        )
        return result

    def _restore_completed(self, request: ToolRequest, idempotency_key: str) -> ToolExecutionResult | None:
        """Recover a completed invocation after the current process was replaced."""

        self.event_store.verify_chain()
        events = self.event_store.read_all()
        completion = next(
            (
                event
                for event in reversed(events)
                if event.run_id == request.run_id
                and event.change_id == request.change_id
                and event.event_type == "ToolInvocationCompleted"
                and event.payload.get("idempotency_key") == idempotency_key
            ),
            None,
        )
        if completion is None:
            return None
        payload = completion.payload
        decision_payload = next(
            (
                event.payload
                for event in reversed(events)
                if event.run_id == request.run_id
                and event.change_id == request.change_id
                and event.event_type == "PolicyDecisionMade"
                and event.payload.get("invocation_id") == payload.get("invocation_id")
            ),
            None,
        )
        if decision_payload is None:
            raise ValueError("completed tool invocation has no persisted policy decision")
        try:
            decision = PolicyDecision(
                decision_id=str(decision_payload["decision_id"]),
                decision=PolicyDecisionType(str(decision_payload["decision"])),
                rule_id=str(decision_payload["rule_id"]),
                reason=str(decision_payload["reason"]),
            )
            exit_code = payload.get("exit_code")
            output = None if exit_code is None else ToolExecutionOutput(
                exit_code=int(exit_code),
                stdout=str(payload.get("stdout") or ""),
                stderr=str(payload.get("stderr") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid persisted tool invocation result") from exc
        return ToolExecutionResult(
            invocation_id=str(payload["invocation_id"]),
            idempotency_key=idempotency_key,
            decision=decision,
            executed=bool(payload.get("executed", True)),
            reused=True,
            output=output,
            error_code=str(payload["error_code"]) if payload.get("error_code") else None,
        )

    def _emit(self, event_type: str, request: ToolRequest, payload: dict[str, object]) -> None:
        if self.event_store is None:
            return
        self.event_store.append(
            EventEnvelope.create(
                event_type=event_type,
                run_id=request.run_id,
                change_id=request.change_id,
                stage_run_id=request.stage_run_id,
                actor=request.actor,
                payload=payload,
            )
        )

    @staticmethod
    def _default_key(request: ToolRequest) -> str:
        normalized = json.dumps(
            {"tool_name": request.tool_name, "args": request.args, "workspace": str(request.workspace_root.resolve())},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def _redact(value: str) -> str:
    redacted = value
    for marker in ("Authorization:", "Bearer ", "api_key=", "api-key="):
        if marker in redacted:
            prefix, _separator, _secret = redacted.partition(marker)
            redacted = f"{prefix}{marker}<REDACTED>"
    return redacted


def _command_environment(workspace_root: Path) -> dict[str, str]:
    """Keep the caller environment and make a src-layout project importable."""
    environment = os.environ.copy()
    source_root = workspace_root / "src"
    if source_root.is_dir():
        current = environment.get("PYTHONPATH")
        paths = [str(source_root)]
        if current:
            paths.append(current)
        environment["PYTHONPATH"] = os.pathsep.join(paths)
    return environment


def _truncate(value: str, limit: int = 8192) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated by VAF]"
