import unittest
import tempfile
from pathlib import Path

from vaf.domain.gates import (
    GateDecision,
    evaluate_artifact_gate,
    evaluate_code_gate,
    validate_domain_contract,
)


class GateTests(unittest.TestCase):
    def test_valid_prd_passes_only_above_strict_threshold(self) -> None:
        content = """---
artifact_id: PRD-CHG-001
artifact_type: prd
change_id: CHG-001
version: 1
status: waiting_review
created_by: test
created_at: 2026-08-05T00:00:00Z
---

# PRD

## 问题与目标

需求目标。

## REQ-001

WHEN 用户提交需求
THE SYSTEM SHALL 返回结果

## 验收条件

- AC-001：通过自动化测试验证。
"""
        result = evaluate_artifact_gate("prd", content)
        self.assertEqual(result.decision, GateDecision.PASS)
        self.assertTrue(result.passed)
        self.assertGreater(result.score, 90)

    def test_missing_requirement_is_blocked(self) -> None:
        content = """---
artifact_id: PRD-CHG-001
artifact_type: prd
change_id: CHG-001
version: 1
status: waiting_review
created_by: test
created_at: 2026-08-05T00:00:00Z
---

## 问题与目标

## 验收条件

AC-001 需要验证。
"""
        result = evaluate_artifact_gate("prd", content)
        self.assertEqual(result.decision, GateDecision.BLOCKED)
        self.assertFalse(result.passed)
        self.assertTrue(any(finding.severity == "P0" for finding in result.findings))

    def test_critical_unresolved_item_blocks_even_with_complete_shape(self) -> None:
        content = """---
artifact_id: PRD-CHG-001
artifact_type: prd
change_id: CHG-001
version: 1
status: waiting_review
created_by: test
created_at: 2026-08-05T00:00:00Z
---

## 问题与目标

目标。

## REQ-001

WHEN 用户提交需求
THE SYSTEM SHALL 返回结果

## 验收条件

- AC-001：验证结果。

[BLOCKED] 关键业务规则尚未确认。
"""
        result = evaluate_artifact_gate("prd", content)
        self.assertEqual(result.decision, GateDecision.BLOCKED)

    def test_code_gate_blocks_high_risk_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text("value = eval(user_input)\n", encoding="utf-8")
            result = evaluate_code_gate(
                worktree_path=root,
                changed_paths=("generated.py",),
                declared_paths={"generated.py"},
                planned_lines=1,
                acceptance_ratio=1.0,
                code_explainability_ratio=1.0,
                validation_errors=[],
                verification_exit_code=0,
            )
            self.assertEqual(result.decision, GateDecision.BLOCKED)
            self.assertTrue(any(finding.criterion_id == "SECURITY" for finding in result.findings))

    def test_code_gate_penalizes_large_change_against_small_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text("\n".join(f"VALUE_{index} = {index}" for index in range(250)), encoding="utf-8")
            result = evaluate_code_gate(
                worktree_path=root,
                changed_paths=("generated.py",),
                declared_paths={"generated.py"},
                planned_lines=1,
                acceptance_ratio=1.0,
                code_explainability_ratio=1.0,
                validation_errors=[],
                verification_exit_code=0,
            )
            self.assertEqual(result.decision, GateDecision.NEEDS_CHANGES)
            self.assertTrue(any(finding.criterion_id == "MINIMALITY" for finding in result.findings))

    def test_commerce_contract_rejects_trace_only_stub(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("@app.get('/api/health')\ndef health(): pass\n", encoding="utf-8")
            errors = validate_domain_contract(
                "AI 搜索选品、货到付款、AI 智能客服",
                root,
                ("main.py",),
            )
            self.assertIn("商城 PRD 契约缺少：AI 选品接口", errors)
            self.assertIn("商城 PRD 契约缺少：AI 客服接口", errors)
