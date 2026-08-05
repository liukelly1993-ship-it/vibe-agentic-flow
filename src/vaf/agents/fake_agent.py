"""Deterministic agent used to validate the workflow kernel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Mapping

from vaf.ports.agents import CodeChange, CodeGenerationResult


@dataclass(frozen=True)
class DraftResult:
    artifact_type: str
    content: str
    assumptions: tuple[str, ...]
    questions: tuple[str, ...]


class FakeAgent:
    def draft_prd(self, change_id: str, title: str, objective: str, version: int = 1) -> DraftResult:
        now = datetime.now(timezone.utc).isoformat()
        content = f"""---
artifact_id: PRD-{change_id}
artifact_type: prd
change_id: {change_id}
version: {version}
status: waiting_review
requirements: [REQ-001]
created_by: fake-product-agent
created_at: {now}
---

# PRD：{title}

## 问题与目标

{objective}

## REQ-001：核心行为

WHEN 用户提出该需求
THE SYSTEM SHALL 提供可验证的实现结果

## 验收条件

- AC-001：核心行为可以通过自动化测试验证。
- AC-002：失败时保留真实错误证据，不生成虚假成功结论。
"""
        return DraftResult(
            artifact_type="prd",
            content=content,
            assumptions=("具体业务角色和非目标需要人工确认",),
            questions=("是否接受当前核心行为和验收口径？",),
        )

    def draft_artifact(
        self,
        artifact_type: str,
        change_id: str,
        title: str,
        objective: str,
        version: int = 1,
    ) -> DraftResult:
        if artifact_type == "prd":
            return self.draft_prd(change_id, title, objective, version)
        now = datetime.now(timezone.utc).isoformat()
        headings = {
            "technical-design": "技术方案",
            "test-cases": "测试用例",
            "implementation-plan": "实施计划",
        }
        body = {
            "technical-design": (
                "## 设计目标\n\n"
                f"围绕 `{title}` 实现：{objective}\n\n"
                "## 影响范围\n\n待由架构负责人确认模块、接口和数据影响。\n\n"
                "## 风险与验证\n\n所有关键行为必须有自动化测试证据。"
            ),
            "test-cases": (
                "## 测试范围\n\n"
                "覆盖核心行为、异常输入和边界条件。\n\n"
                "## TC-001\n\n"
                "前置条件：测试环境可用。\n\n步骤：执行核心行为。\n\n预期：得到可验证结果。"
            ),
            "implementation-plan": (
                "## TASK-001\n\n"
                "实现核心行为并补充自动化测试。\n\n"
                "完成条件：代码、测试和 TraceLink 均通过校验。"
            ),
        }[artifact_type]
        content = f"""---
artifact_id: {artifact_type.upper().replace('-', '-')}-{change_id}-V{version}
artifact_type: {artifact_type}
change_id: {change_id}
version: {version}
status: waiting_review
requirements: [REQ-001]
created_by: fake-{artifact_type}-agent
created_at: {now}
---

# {headings[artifact_type]}：{title}

{body}
"""
        return DraftResult(
            artifact_type=artifact_type,
            content=content,
            assumptions=("具体实现边界需要结合目标仓库上下文确认",),
            questions=("是否接受当前阶段产物？",),
        )

    def generate_code(
        self,
        *,
        change_id: str,
        title: str,
        objective: str,
        implementation: Mapping[str, object],
    ) -> CodeGenerationResult:
        """Turn a declared file plan into deterministic candidate changes."""

        raw_changes = implementation.get("changes")
        if not isinstance(raw_changes, list) or not raw_changes:
            raise ValueError("implementation plan requires a non-empty changes list")
        changes: list[CodeChange] = []
        for item in raw_changes:
            if not isinstance(item, dict):
                raise ValueError("each implementation change must be a mapping")
            task_id = str(item.get("task_id", "")).strip()
            path = str(item.get("path", "")).strip()
            content = item.get("content")
            if not task_id or not path or not isinstance(content, str):
                raise ValueError("each implementation change requires task_id, path, and string content")
            normalized = PurePosixPath(path)
            if normalized.is_absolute() or ".." in normalized.parts:
                raise ValueError(f"implementation path must stay relative to the worktree: {path}")
            requirement_ids = _string_list(item.get("requirement_ids"))
            acceptance_ids = _string_list(item.get("acceptance_ids"))
            test_ids = _string_list(item.get("test_ids"))
            changes.append(
                CodeChange(
                    task_id=task_id,
                    path=str(normalized),
                    content=content,
                    requirement_ids=requirement_ids,
                    acceptance_ids=acceptance_ids,
                    test_ids=test_ids,
                )
            )
        return CodeGenerationResult(
            changes=tuple(changes),
            assumptions=(f"由 FakeAgent 按已声明的实施文件计划生成：{title}",),
            questions=(f"是否确认变更目标与代码文件范围符合：{objective}？",),
        )


def _string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError("trace mapping fields must be lists of non-empty strings")
    return tuple(value)
