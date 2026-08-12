import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vaf.adapters.minimax import MiniMaxClient, MiniMaxConfig, MiniMaxProviderError
from vaf.ports.agents import ImageInput


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/3kY2AAAAAElFTkSuQmCC"
)


class MiniMaxClientTests(unittest.TestCase):
    def test_multimodal_tool_call_uses_anthropic_image_block_and_hash_evidence(self) -> None:
        captured: dict[str, object] = {}

        def transport(config: MiniMaxConfig, payload: dict[str, object]) -> dict[str, object]:
            captured.update(payload)
            return {
                "id": "msg-test",
                "model": config.model,
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [
                    {"type": "tool_use", "name": "inspect", "input": {"status": "ok"}}
                ],
            }

        config = MiniMaxConfig(api_key="secret-value", model="MiniMax-M3")
        client = MiniMaxClient(config, transport=transport)
        image = ImageInput(
            source_name="prototype.png",
            media_type="image/png",
            data=PNG_BYTES,
            content_hash="sha256:image-test",
        )
        with tempfile.TemporaryDirectory() as directory:
            result = client.invoke_tool(
                system="system",
                prompt="inspect prototype",
                tool_name="inspect",
                input_schema={
                    "type": "object",
                    "properties": {"status": {"type": "string"}},
                    "required": ["status"],
                },
                max_tokens=512,
                workspace_root=Path(directory),
                images=(image,),
            )

        messages = captured["messages"]
        content = messages[0]["content"]  # type: ignore[index]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["type"], "base64")
        self.assertEqual(content[0]["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(content[0]["source"]["data"]), PNG_BYTES)
        self.assertEqual(content[-1], {"type": "text", "text": "inspect prototype"})
        self.assertEqual(result.input_image_hashes, ("sha256:image-test",))
        self.assertTrue(result.evidence()["multimodal"])
        self.assertNotIn("secret-value", repr(config))
        self.assertNotIn("secret-value", str(result.evidence()))

    def test_https_image_url_uses_url_source(self) -> None:
        captured: dict[str, object] = {}

        def transport(_config: MiniMaxConfig, payload: dict[str, object]) -> dict[str, object]:
            captured.update(payload)
            return {
                "id": "msg-url",
                "model": "MiniMax-M3",
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "inspect", "input": {"status": "ok"}}],
            }

        client = MiniMaxClient(MiniMaxConfig(api_key="secret-value"), transport=transport)
        image = ImageInput(
            source_name="prototype-url",
            url="https://cdn.example.com/prototype.png",
            content_hash="sha256:url-test",
        )
        with tempfile.TemporaryDirectory() as directory:
            client.invoke_tool(
                system="system",
                prompt="inspect",
                tool_name="inspect",
                input_schema={"type": "object"},
                max_tokens=512,
                workspace_root=Path(directory),
                images=(image,),
            )
        content = captured["messages"][0]["content"]  # type: ignore[index]
        self.assertEqual(
            content[0]["source"],
            {"type": "url", "url": "https://cdn.example.com/prototype.png"},
        )

    def test_invalid_image_media_type_is_rejected_before_transport(self) -> None:
        client = MiniMaxClient(
            MiniMaxConfig(api_key="secret-value"),
            transport=lambda _config, _payload: self.fail("transport must not run"),
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MiniMaxProviderError, "格式不受支持"):
                client.invoke_tool(
                    system="system",
                    prompt="inspect",
                    tool_name="inspect",
                    input_schema={"type": "object"},
                    max_tokens=512,
                    workspace_root=Path(directory),
                    images=(
                        ImageInput(
                            source_name="bad.svg",
                            media_type="image/svg+xml",
                            data=b"<svg/>",
                            content_hash="sha256:bad",
                        ),
                    ),
                )

    def test_environment_configuration_defaults_to_china_m3_endpoint(self) -> None:
        with patch.dict("os.environ", {"MINIMAX_API_KEY": "secret-value"}, clear=True):
            config = MiniMaxConfig.from_env()
        self.assertEqual(config.model, "MiniMax-M3")
        self.assertEqual(config.base_url, "https://api.minimaxi.com/anthropic")
        self.assertTrue(config.public_dict()["multimodal"])


if __name__ == "__main__":
    unittest.main()
