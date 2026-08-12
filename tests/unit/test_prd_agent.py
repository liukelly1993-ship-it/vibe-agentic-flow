import unittest

from vaf.domain.gates import GateDecision, evaluate_artifact_gate
from vaf.web.prd_agent import PrdContext, PrdTemplateAgent
from vaf.web.stacks import choose_stack


class PrdAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        source = """# PRD：商城 MVP

AI 搜索选品、商品浏览、购物车、货到付款、订单状态、AI 智能客服、商家后台。
"""
        self.agent = PrdTemplateAgent(
            PrdContext(
                title="商城 MVP",
                objective="验证商城核心闭环",
                source_text=source,
                source_hash="sha256:test",
                stack=choose_stack(source),
                has_visual_evidence=True,
            )
        )

    def test_commerce_prd_contains_domain_requirements_and_acceptance(self) -> None:
        artifact = self.agent.draft_prd("CHG-COMMERCE", "商城 MVP", "验证商城核心闭环")
        for identifier in ("REQ-001", "REQ-006", "AC-001", "AC-011"):
            self.assertIn(identifier, artifact.content)
        self.assertIn("默认 COD", artifact.content)
        self.assertIn("不出现在线支付", artifact.content)
        gate = evaluate_artifact_gate("prd", artifact.content, source_visual_evidence=True)
        self.assertEqual(gate.decision, GateDecision.PASS)

    def test_commerce_test_cases_include_regression_and_unexecuted_e2e_boundary(self) -> None:
        artifact = self.agent.draft_artifact(
            "test-cases",
            "CHG-COMMERCE",
            "商城 MVP",
            "验证商城核心闭环",
        )
        for identifier in ("TC-001", "TC-003", "TC-007", "TC-012"):
            self.assertIn(identifier, artifact.content)
        self.assertIn("回归用例清单", artifact.content)
        self.assertIn("浏览器 E2E", artifact.content)
        self.assertIn("不得计入通过分数", artifact.content)

    def test_commerce_implementation_trace_only_declares_executable_backend_cases(self) -> None:
        items = self.agent.implementation_items()
        paths = {str(item["path"]) for item in items}
        self.assertIn("compose.yaml", paths)
        self.assertIn("frontend/src/App.vue", paths)
        self.assertNotIn("frontend/src/App.jsx", paths)
        backend_test = next(item for item in items if item["path"] == "tests/test_backend.py")
        self.assertEqual(
            backend_test["test_ids"],
            ["TC-001", "TC-003", "TC-007", "TC-008", "TC-009"],
        )
        self.assertIn("test_order_status_rejects_invalid_values", str(backend_test["content"]))
        self.assertNotIn("TC-004", backend_test["test_ids"])

    def test_prd_records_knowledge_snapshot_and_source_hashes(self) -> None:
        source = "# Internal integration\n\n现有 CRM 接口需要可信知识依据。"
        agent = PrdTemplateAgent(
            PrdContext(
                title="Knowledge grounded change",
                objective="按现有 CRM 契约实现",
                source_text=source,
                source_hash="sha256:source",
                stack=choose_stack(source),
                has_visual_evidence=True,
                knowledge_snapshot_hash="sha256:snapshot",
                knowledge_documents=(("crm-api.md", "sha256:document", "CRM 客户接口错误码为 CRM-001。"),),
            )
        )
        artifact = agent.draft_prd("CHG-KB", "Knowledge grounded change", "按现有 CRM 契约实现")
        self.assertIn("knowledge_snapshot_hash: sha256:snapshot", artifact.content)
        self.assertIn("KB-001", artifact.content)
        self.assertIn("sha256:document", artifact.content)


if __name__ == "__main__":
    unittest.main()
