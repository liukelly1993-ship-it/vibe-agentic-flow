import unittest
from pathlib import Path

from vaf.adapters.minimax import MiniMaxConfig, MiniMaxProviderError, MiniMaxToolResult
from vaf.domain.gates import GateDecision, evaluate_artifact_gate
from vaf.ports.agents import ImageInput
from vaf.web.minimax_agent import MiniMaxAgent
from vaf.web.prd_agent import PrdContext
from vaf.web.stacks import StackChoice


class StubMiniMaxClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def invoke_tool(self, **kwargs) -> MiniMaxToolResult:
        self.calls.append(kwargs)
        if kwargs["tool_name"] == "submit_prototype_analysis":
            value = {
                "has_usable_prototype": True,
                "inspected_image_count": 1,
                "prototype_summary": "这是一个任务看板原型，顶部有标题、新建按钮和筛选器，中部按状态分列展示任务卡片，底部没有额外导航。",
                "screens": [
                    {
                        "image_index": 1,
                        "screen_name": "任务看板",
                        "visible_text": ["任务看板"],
                        "layout": "顶部工具栏和三列看板",
                        "controls": ["新建任务", "负责人筛选"],
                        "interaction_clues": ["任务卡片可切换状态"],
                        "uncertainties": ["拖拽行为无法从静态图确认"],
                    }
                ],
            }
        else:
            value = {
                "body": """# PRD：任务看板

## 问题与目标
目标是提供可本地验收的任务看板。

## 范围与非目标
仅实现任务创建、查看和状态切换，不实现聊天。

## REQ-001
WHEN 用户创建任务 THE SYSTEM SHALL 保存并展示任务。

## 验收条件
- AC-001：通过自动化测试验证创建后返回 ID 并在页面展示。

## 原型证据
任务看板采用顶部工具栏与三列卡片布局，拖拽能力仍属不确定项。

## 数据与 API
使用任务 API 和数据库持久化。

## 非功能与安全
输入需要校验，不读取或输出密钥。

## 风险与假设
静态原型无法确认全部动态交互。

## 知识依据
本需求不依赖外部知识库。
""",
                "assumptions": [],
                "questions": [],
            }
        return MiniMaxToolResult(
            value=value,
            model="MiniMax-M3",
            request_id=f"msg-{len(self.calls)}",
            stop_reason="tool_use",
            usage={},
            output_hash=f"sha256:output-{len(self.calls)}",
            input_image_hashes=("sha256:prototype",),
        )


def context_with_image() -> PrdContext:
    return PrdContext(
        title="任务看板",
        objective="实现任务看板",
        source_text="REQ-001 创建任务。AC-001 创建后返回 ID。",
        source_hash="sha256:source",
        stack=StackChoice(
            backend="FastAPI",
            frontend="Vue 3 + Vite",
            database="PostgreSQL",
            reason="test",
        ),
        has_visual_evidence=True,
        visual_inputs=(
            ImageInput(
                source_name="prototype.png",
                media_type="image/png",
                data=b"\x89PNG\r\n\x1a\nfixture",
                content_hash="sha256:prototype",
            ),
        ),
    )


class MiniMaxAgentTests(unittest.TestCase):
    def test_prd_binds_m3_visual_analysis_to_artifact_gate(self) -> None:
        client = StubMiniMaxClient()
        agent = MiniMaxAgent(
            context_with_image(),
            config=MiniMaxConfig(api_key="secret-value", model="MiniMax-M3"),
            client=client,  # type: ignore[arg-type]
            workspace_root=Path.cwd(),
        )
        artifact = agent.draft_prd("CHG-001", "任务看板", "实现任务看板")
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call["images"] for call in client.calls))
        self.assertIn("prototype_understood_by_model: true", artifact.content)
        self.assertIn("prototype_analysis_hash: sha256:", artifact.content)
        gate = evaluate_artifact_gate("prd", artifact.content, source_visual_evidence=True)
        self.assertEqual(gate.decision, GateDecision.PASS)
        evidence = agent.evidence()[0]
        self.assertEqual(evidence["stage"], "prototype-inspection")
        self.assertTrue(evidence["multimodal"])

    def test_agent_refuses_to_claim_visual_understanding_without_image(self) -> None:
        context = context_with_image()
        context = PrdContext(
            title=context.title,
            objective=context.objective,
            source_text=context.source_text,
            source_hash=context.source_hash,
            stack=context.stack,
        )
        agent = MiniMaxAgent(
            context,
            config=MiniMaxConfig(api_key="secret-value", model="MiniMax-M3"),
            client=StubMiniMaxClient(),  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(MiniMaxProviderError, "没有可发送"):
            agent.draft_prd("CHG-001", "任务看板", "实现任务看板")


if __name__ == "__main__":
    unittest.main()
