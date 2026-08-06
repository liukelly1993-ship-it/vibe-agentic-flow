"""Deterministic quality gates for versioned workflow artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import ast
from pathlib import Path
import re

from vaf.domain.artifacts import ArtifactVersion


class GateType(StrEnum):
    PRODUCT = "product"
    SOLUTION = "solution"
    QUALITY = "quality"


class GateDecision(StrEnum):
    PASS = "PASS"
    NEEDS_CHANGES = "NEEDS_CHANGES"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class GateCriterion:
    criterion_id: str
    label: str
    weight: float
    passed: bool
    blocking: bool
    evidence: str


@dataclass(frozen=True)
class GateFinding:
    finding_id: str
    severity: str
    criterion_id: str
    message: str
    correction: str


@dataclass(frozen=True)
class GateResult:
    gate_type: GateType
    artifact_type: str
    target_hash: str | None
    score: float
    threshold: float
    decision: GateDecision
    criteria: tuple[GateCriterion, ...]
    findings: tuple[GateFinding, ...]

    @property
    def passed(self) -> bool:
        return self.decision == GateDecision.PASS and self.score > self.threshold

    def to_dict(self) -> dict[str, object]:
        return {
            "gate_type": self.gate_type.value,
            "artifact_type": self.artifact_type,
            "target_hash": self.target_hash,
            "score": self.score,
            "threshold": self.threshold,
            "decision": self.decision.value,
            "passed": self.passed,
            "criteria": [criterion.__dict__ for criterion in self.criteria],
            "findings": [finding.__dict__ for finding in self.findings],
        }


def evaluate_artifact_gate(
    artifact_type: str,
    content: str,
    *,
    target_hash: str | None = None,
    threshold: float = 90.0,
) -> GateResult:
    """Evaluate an artifact before automatic stage promotion.

    This is intentionally deterministic. It validates structure and evidence
    anchors; it does not treat an LLM's self-assessment as proof.
    """

    gate_type = GateType.PRODUCT if artifact_type == "prd" else GateType.SOLUTION
    findings: list[GateFinding] = []
    criteria: list[GateCriterion] = []

    try:
        artifact = ArtifactVersion.from_markdown(content)
        schema_passed = artifact.artifact_type.value == artifact_type
        schema_evidence = "frontmatter and artifact type are valid"
    except ValueError as exc:
        artifact = None
        schema_passed = False
        schema_evidence = str(exc)

    criteria.append(
        GateCriterion("SCHEMA", "产物结构和类型", 25.0, schema_passed, True, schema_evidence)
    )
    if not schema_passed:
        findings.append(
            GateFinding(
                "GATE-SCHEMA-001",
                "P0",
                "SCHEMA",
                "产物 frontmatter 或 artifact_type 不合法",
                "修复 Schema 后重新生成并评审当前版本",
            )
        )

    requirements = _required_patterns(artifact_type)
    object_passed = all(re.search(pattern, content, re.IGNORECASE) for pattern in requirements["objects"])
    object_evidence = "、".join(requirements["object_labels"]) if object_passed else "缺少：" + "、".join(
        label for label, pattern in zip(requirements["object_labels"], requirements["objects"])
        if not re.search(pattern, content, re.IGNORECASE)
    )
    criteria.append(
        GateCriterion("OBJECTS", "阶段对象引用", 25.0, object_passed, True, object_evidence)
    )
    if not object_passed:
        findings.append(
            GateFinding(
                "GATE-OBJECT-001",
                "P0",
                "OBJECTS",
                "缺少当前阶段必需的稳定对象 ID",
                "补充 REQ、AC、TC 或 TASK 等可追踪对象",
            )
        )

    sections_passed = all(re.search(pattern, content, re.IGNORECASE) for pattern in requirements["sections"])
    section_evidence = "关键章节齐全" if sections_passed else "缺少关键章节"
    criteria.append(
        GateCriterion("SECTIONS", "关键章节完整性", 20.0, sections_passed, True, section_evidence)
    )
    if not sections_passed:
        findings.append(
            GateFinding(
                "GATE-SECTION-001",
                "P0",
                "SECTIONS",
                "产物缺少当前阶段要求的关键章节",
                "按阶段模板补齐目标、风险、测试或完成条件",
            )
        )

    evidence_passed = bool(re.search(requirements["evidence"], content, re.IGNORECASE))
    criteria.append(
        GateCriterion(
            "EVIDENCE",
            "可验证性",
            20.0,
            evidence_passed,
            True,
            "存在可执行或可检查的验证描述" if evidence_passed else "未找到可验证描述",
        )
    )
    if not evidence_passed:
        findings.append(
            GateFinding(
                "GATE-EVIDENCE-001",
                "P0",
                "EVIDENCE",
                "产物没有提供可执行的验证口径",
                "补充测试、验收、验证或完成条件",
            )
        )

    reviewability_passed = bool(content.strip()) and not bool(re.search(r"\b(?:TODO|TBD)\b|待补充", content, re.IGNORECASE))
    criteria.append(
        GateCriterion(
            "REVIEWABILITY",
            "评审可读性",
            10.0,
            reviewability_passed,
            False,
            "没有未处理模板占位符" if reviewability_passed else "存在未处理占位符",
        )
    )
    if not reviewability_passed:
        findings.append(
            GateFinding(
                "GATE-REVIEW-001",
                "P1",
                "REVIEWABILITY",
                "产物仍包含 TODO、TBD 或待补充占位符",
                "明确处理、删除或转为待确认问题",
            )
        )

    if re.search(r"\[BLOCKED\]|CRITICAL_UNRESOLVED", content, re.IGNORECASE):
        findings.append(
            GateFinding(
                "GATE-BLOCK-001",
                "P0",
                "REVIEWABILITY",
                "产物包含未解决的关键阻塞项",
                "补充可信来源或明确输入，不得直接生成下游产物",
            )
        )

    score = round(sum(criterion.weight for criterion in criteria if criterion.passed), 2)
    hard_failures = any(finding.severity == "P0" for finding in findings)
    if hard_failures:
        decision = GateDecision.BLOCKED
    elif score > threshold:
        decision = GateDecision.PASS
    else:
        decision = GateDecision.NEEDS_CHANGES
    return GateResult(
        gate_type=gate_type,
        artifact_type=artifact_type,
        target_hash=target_hash,
        score=score,
        threshold=threshold,
        decision=decision,
        criteria=tuple(criteria),
        findings=tuple(findings),
    )


def evaluate_code_gate(
    *,
    worktree_path: str | Path | None,
    changed_paths: tuple[str, ...],
    declared_paths: set[str],
    planned_lines: int,
    acceptance_ratio: float,
    code_explainability_ratio: float,
    validation_errors: list[str],
    verification_exit_code: int | None,
    target_hash: str | None = None,
) -> GateResult:
    """Score generated code with deterministic, explainable heuristics."""

    criteria: list[GateCriterion] = []
    findings: list[GateFinding] = []
    root = Path(worktree_path) if worktree_path else None
    files: list[tuple[str, str]] = []
    for relative_path in changed_paths:
        if root is None:
            continue
        path = root / relative_path
        try:
            files.append((relative_path, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue

    security_patterns = (
        (r"\beval\s*\(", "动态 eval"),
        (r"\bexec\s*\(", "动态 exec"),
        (r"\bshell\s*=\s*True\b", "shell=True"),
        (r"\bos\.system\s*\(", "os.system"),
        (r"\bpickle\.loads\s*\(", "不安全反序列化"),
        (r"(?i)\b(?:api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]{8,}['\"]", "疑似硬编码凭据"),
    )
    security_hits = [
        label
        for _relative_path, content in files
        for pattern, label in security_patterns
        if re.search(pattern, content)
    ]
    security_passed = not security_hits
    criteria.append(
        GateCriterion(
            "SECURITY",
            "安全风险",
            20.0,
            security_passed,
            True,
            "未发现高风险调用" if security_passed else "、".join(sorted(set(security_hits))),
        )
    )
    if not security_passed:
        findings.append(
            GateFinding(
                "CODE-SECURITY-001",
                "P0",
                "SECURITY",
                "代码包含高风险执行、反序列化或疑似硬编码凭据",
                "删除危险调用，改用受控适配器或 SecretReference",
            )
        )

    requirement_fit_passed = (
        not validation_errors
        and acceptance_ratio == 1.0
        and code_explainability_ratio == 1.0
    )
    criteria.append(
        GateCriterion(
            "REQUIREMENT_FIT",
            "需求和 Trace 贴合度",
            20.0,
            requirement_fit_passed,
            True,
            "REQ/AC/TC/TASK 链路和哈希均有效" if requirement_fit_passed else "存在追踪或覆盖率缺口",
        )
    )
    if not requirement_fit_passed:
        findings.append(
            GateFinding(
                "CODE-TRACE-001",
                "P0",
                "REQUIREMENT_FIT",
                "代码没有完整对应批准的需求、验收条件或 Trace 证据",
                "补齐显式映射并重新执行验证",
            )
        )

    changed_set = set(changed_paths)
    unexpected = sorted(changed_set - declared_paths)
    scope_passed = not unexpected and bool(changed_set)
    criteria.append(
        GateCriterion(
            "SCOPE",
            "影响范围控制",
            15.0,
            scope_passed,
            True,
            "所有变更文件均在任务声明范围内" if scope_passed else f"越界或没有代码变更：{unexpected}",
        )
    )
    if not scope_passed:
        findings.append(
            GateFinding(
                "CODE-SCOPE-001",
                "P0",
                "SCOPE",
                f"发现任务范围外文件或没有可审计的代码变更：{unexpected}",
                "缩小变更范围或更新经过评审的实施计划",
            )
        )

    verification_passed = verification_exit_code == 0
    criteria.append(
        GateCriterion(
            "TEST_EVIDENCE",
            "测试和验证证据",
            20.0,
            verification_passed,
            True,
            "验证命令退出码为 0" if verification_passed else f"验证退出码：{verification_exit_code}",
        )
    )
    if not verification_passed:
        findings.append(
            GateFinding(
                "CODE-TEST-001",
                "P0",
                "TEST_EVIDENCE",
                "自动验证没有成功通过",
                "修复代码或测试后重新执行验证，不允许生成成功结论",
            )
        )

    syntax_passed, long_functions = _check_python_structure(files)
    architecture_passed = syntax_passed and not long_functions
    architecture_evidence = "Python 语法和函数规模正常"
    if not syntax_passed:
        architecture_evidence = "存在 Python 语法错误"
    elif long_functions:
        architecture_evidence = f"函数过长：{', '.join(long_functions)}"
    criteria.append(
        GateCriterion("ARCHITECTURE", "结构和可维护性", 10.0, architecture_passed, not syntax_passed, architecture_evidence)
    )
    if not architecture_passed:
        findings.append(
            GateFinding(
                "CODE-ARCH-001",
                "P0" if not syntax_passed else "P1",
                "ARCHITECTURE",
                "代码结构存在语法错误或过长函数",
                "先修复语法；再拆分复杂函数并保持职责单一",
            )
        )

    actual_lines = sum(len(content.splitlines()) for _relative_path, content in files)
    line_budget = max(planned_lines * 2, planned_lines + 100, 1)
    minimality_passed = actual_lines <= line_budget and len(changed_set) <= max(len(declared_paths), 1)
    minimality_evidence = f"实际 {actual_lines} 行，计划基线 {planned_lines} 行，允许上限 {line_budget} 行"
    criteria.append(
        GateCriterion("MINIMALITY", "最小改动和反过度设计", 10.0, minimality_passed, False, minimality_evidence)
    )
    if not minimality_passed:
        findings.append(
            GateFinding(
                "CODE-SIZE-001",
                "P1",
                "MINIMALITY",
                "实际改动相对实施计划过大，可能存在过度设计或无关修改",
                "拆分任务、删除无关抽象并缩小代码变更",
            )
        )

    comment_lines = sum(
        1
        for relative_path, content in files
        if relative_path.endswith(".py")
        for line in content.splitlines()
        if line.strip().startswith("#")
    )
    code_lines = sum(
        1
        for relative_path, content in files
        if relative_path.endswith(".py")
        for line in content.splitlines()
        if line.strip()
    )
    verbosity_passed = code_lines == 0 or comment_lines / code_lines <= 0.35
    criteria.append(
        GateCriterion(
            "CONCISION",
            "代码简洁度",
            5.0,
            verbosity_passed,
            False,
            "注释比例处于合理范围" if verbosity_passed else "注释或样板代码比例过高",
        )
    )
    if not verbosity_passed:
        findings.append(
            GateFinding(
                "CODE-VERBOSE-001",
                "P1",
                "CONCISION",
                "代码包含过多注释或样板内容，降低有效信息密度",
                "删除重复解释，保留必要的设计原因和边界说明",
            )
        )

    score = round(sum(criterion.weight for criterion in criteria if criterion.passed), 2)
    hard_failures = any(finding.severity == "P0" for finding in findings)
    if hard_failures:
        decision = GateDecision.BLOCKED
    elif score > 90.0:
        decision = GateDecision.PASS
    else:
        decision = GateDecision.NEEDS_CHANGES
    return GateResult(
        gate_type=GateType.QUALITY,
        artifact_type="implementation",
        target_hash=target_hash,
        score=score,
        threshold=90.0,
        decision=decision,
        criteria=tuple(criteria),
        findings=tuple(findings),
    )


def validate_domain_contract(
    source_text: str,
    worktree_path: str | Path | None,
    changed_paths: tuple[str, ...],
) -> list[str]:
    """Check high-signal PRD contracts against the generated source tree.

    Trace metadata alone cannot prove that a generated project implements a
    requirement. This check stays deliberately narrow: it activates for the
    known commerce MVP contract and requires its externally visible seams.
    Generic PRDs continue to use the structural and test gates.
    """

    normalized = source_text.lower()
    commerce_signals = (
        ("ai 搜索选品" in normalized or "/api/ai-search" in normalized),
        ("货到付款" in normalized or "cod" in normalized),
        ("ai 智能客服" in normalized or "/api/ai-chat" in normalized),
    )
    if not all(commerce_signals):
        return []
    root = Path(worktree_path) if worktree_path else None
    if root is None:
        return ["商城 PRD 需要工作区证据，但当前没有生成工作区"]
    corpus_parts: list[str] = []
    for relative_path in changed_paths:
        path = root / relative_path
        try:
            corpus_parts.append(path.read_text(encoding="utf-8").lower())
        except (OSError, UnicodeDecodeError):
            continue
    corpus = "\n".join(corpus_parts)
    contracts = {
        "AI 选品接口": ("/api/ai-search", "ai_search"),
        "商品接口": ("/api/products", "products"),
        "订单接口": ("/api/orders", "orders"),
        "默认货到付款": ("货到付款", "cash_on_delivery", "cod"),
        "AI 客服接口": ("/api/ai-chat", "ai_chat"),
    }
    return [
        f"商城 PRD 契约缺少：{label}"
        for label, markers in contracts.items()
        if not any(marker in corpus for marker in markers)
    ]


def _check_python_structure(files: list[tuple[str, str]]) -> tuple[bool, list[str]]:
    long_functions: list[str] = []
    for relative_path, content in files:
        if not relative_path.endswith(".py"):
            continue
        try:
            tree = ast.parse(content, filename=relative_path)
        except SyntaxError:
            return False, []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno:
                if node.end_lineno - node.lineno + 1 > 80:
                    long_functions.append(f"{relative_path}:{node.name}")
    return True, long_functions


def _required_patterns(artifact_type: str) -> dict[str, object]:
    if artifact_type == "prd":
        return {
            "objects": (r"\bREQ-\d+\b", r"\bAC-\d+\b"),
            "object_labels": ("REQ-*", "AC-*"),
            "sections": (r"问题与目标|目标", r"验收条件|验收"),
            "evidence": r"验收|WHEN|THE SYSTEM SHALL",
        }
    if artifact_type == "technical-design":
        return {
            "objects": (r"\bREQ-\d+\b",),
            "object_labels": ("REQ-*",),
            "sections": (r"设计目标|目标", r"影响范围|架构", r"风险与验证|测试"),
            "evidence": r"风险|验证|测试|错误处理",
        }
    if artifact_type == "test-cases":
        return {
            "objects": (r"\bTC-\d+\b",),
            "object_labels": ("TC-*",),
            "sections": (r"测试范围|测试策略",),
            "evidence": r"预期|断言|验证",
        }
    return {
        "objects": (r"\bTASK-\d+\b",),
        "object_labels": ("TASK-*",),
        "sections": (r"完成条件|实施|实现",),
        "evidence": r"完成条件|测试|验证|TraceLink",
    }
