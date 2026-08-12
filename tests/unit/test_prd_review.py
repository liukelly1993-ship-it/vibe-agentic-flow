import base64
import tempfile
import unittest
from pathlib import Path

from vaf.domain.gates import GateDecision
from vaf.web.ingestion import ingest_upload
from vaf.web.prd_review import KnowledgeBaseError, load_knowledge_base, review_source_prd


PROTOTYPE_DATA_URL = "data:image/png;base64," + base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/3kY2AAAAAElFTkSuQmCC"
    )
).decode("ascii")


def complete_prd(*, prototype: bool = True, knowledge_reference: bool = False) -> str:
    reference = "系统需要对接现有 CRM，客户字段和错误码按照内部接口文档执行。" if knowledge_reference else ""
    visual = f"![任务看板原型]({PROTOTYPE_DATA_URL})" if prototype else ""
    return f"""# 任务看板 PRD

## 背景、目标与业务价值

团队需要统一管理任务，目标是减少遗漏并让负责人及时了解工作状态。

## 用户角色和使用场景

项目成员创建和更新任务；项目负责人查看看板、筛选负责人并跟踪逾期任务。

## 范围、非目标和约束

本期范围包含任务创建、编辑、状态流转和筛选。非目标包括聊天、计费和移动客户端。

## 功能需求

- REQ-001：系统应允许用户创建包含标题、负责人、截止日期和状态的任务。
- REQ-002：系统应允许用户更新任务状态并按负责人筛选。

## 验收条件

- AC-001：创建合法任务后 API 返回唯一 ID，页面出现新任务。
- AC-002：缺少标题时 API 返回 422，页面显示字段错误。
- AC-003：切换状态后刷新页面仍显示最新状态。

## 数据和 API

任务包含 ID、标题、负责人、截止日期和状态；通过 REST API 读写。

## 非功能、安全与隐私

普通用户不能修改其他项目数据；关键写操作写入审计日志；本地验收 P95 响应低于 500ms。

## 风险、假设和依赖

假设本期使用本地账号；风险是历史任务数据质量不一致，导入不在本期范围。

## 页面原型

{visual}

{reference}
"""


class PrdSourceReviewTests(unittest.TestCase):
    def test_complete_prd_passes_without_domain_knowledge(self) -> None:
        source = complete_prd()
        document = ingest_upload("prd.md", source.encode("utf-8"))
        result = review_source_prd(document)
        self.assertEqual(result.score, 100.0)
        self.assertEqual(result.decision, GateDecision.PASS)
        self.assertFalse(result.knowledge_request.required)

    def test_missing_prototype_is_p0_blocked_at_85(self) -> None:
        source = complete_prd(prototype=False)
        document = ingest_upload("prd.md", source.encode("utf-8"))
        result = review_source_prd(document)
        self.assertEqual(result.score, 85.0)
        self.assertEqual(result.decision, GateDecision.BLOCKED)
        self.assertIn("PRD-PROTOTYPE-001", [item.finding_id for item in result.findings])

    def test_score_of_exactly_90_does_not_pass(self) -> None:
        source = complete_prd().replace(
            "## 非功能、安全与隐私\n\n普通用户不能修改其他项目数据；关键写操作写入审计日志；本地验收 P95 响应低于 500ms。",
            "",
        )
        document = ingest_upload("prd.md", source.encode("utf-8"))
        result = review_source_prd(document)
        self.assertEqual(result.score, 90.0)
        self.assertEqual(result.decision, GateDecision.NEEDS_CHANGES)
        self.assertFalse(result.passed)

    def test_existing_system_reference_requires_relevant_knowledge(self) -> None:
        source = complete_prd(knowledge_reference=True)
        document = ingest_upload("prd.md", source.encode("utf-8"))
        blocked = review_source_prd(document)
        self.assertEqual(blocked.decision, GateDecision.BLOCKED)
        self.assertTrue(blocked.knowledge_request.required)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            root.mkdir()
            (root / "crm-api.md").write_text(
                "CRM 客户接口文档：客户字段包含 customer_id、name。错误码按照内部规则返回。",
                encoding="utf-8",
            )
            snapshot = load_knowledge_base(root, allowed_roots=(Path(directory),))
            passed = review_source_prd(document, snapshot)
            self.assertEqual(passed.decision, GateDecision.PASS)
            self.assertEqual(passed.knowledge_snapshot.snapshot_hash, snapshot.snapshot_hash)

    def test_knowledge_path_outside_allowlist_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            (Path(other) / "rules.md").write_text("业务规则", encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeBaseError, "允许范围"):
                load_knowledge_base(other, allowed_roots=(Path(directory),))


if __name__ == "__main__":
    unittest.main()
