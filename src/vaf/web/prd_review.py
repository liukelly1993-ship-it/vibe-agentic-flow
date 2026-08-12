"""Deterministic admission review for raw PRDs and local knowledge evidence."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from vaf.adapters.tool_gateway import ToolGateway
from vaf.domain.gates import GateDecision
from vaf.policy.engine import PolicyEngine, ToolRequest
from vaf.ports.tools import ToolExecutionOutput
from vaf.web.ingestion import ALLOWED_EXTENSIONS, IngestedDocument, ingest_upload


MAX_KNOWLEDGE_FILES = 200
MAX_KNOWLEDGE_BYTES = 50 * 1024 * 1024
KNOWLEDGE_EXTENSIONS = ALLOWED_EXTENSIONS | {".csv", ".json", ".yaml", ".yml"}


class KnowledgeBaseError(ValueError):
    """Raised when a local knowledge source is outside its configured boundary."""


@dataclass(frozen=True)
class KnowledgeDocument:
    path: str
    content_hash: str
    text: str

    def public_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "content_hash": self.content_hash,
            "character_count": len(self.text),
        }


@dataclass(frozen=True)
class KnowledgeSnapshot:
    root: str
    snapshot_hash: str
    documents: tuple[KnowledgeDocument, ...]

    @property
    def text(self) -> str:
        return "\n".join(document.text for document in self.documents)

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "snapshot_hash": self.snapshot_hash,
            "document_count": len(self.documents),
            "documents": [document.public_dict() for document in self.documents],
        }


@dataclass(frozen=True)
class PrdReviewCriterion:
    criterion_id: str
    label: str
    weight: float
    score: float
    blocking: bool
    evidence: str


@dataclass(frozen=True)
class PrdReviewFinding:
    finding_id: str
    severity: str
    criterion_id: str
    message: str
    correction: str


@dataclass(frozen=True)
class KnowledgeRequest:
    required: bool
    reasons: tuple[str, ...]
    expected_sources: tuple[str, ...]


@dataclass(frozen=True)
class PrdReviewResult:
    score: float
    threshold: float
    decision: GateDecision
    criteria: tuple[PrdReviewCriterion, ...]
    findings: tuple[PrdReviewFinding, ...]
    knowledge_request: KnowledgeRequest
    knowledge_snapshot: KnowledgeSnapshot | None

    @property
    def passed(self) -> bool:
        return self.decision == GateDecision.PASS and self.score > self.threshold

    def to_dict(self) -> dict[str, object]:
        return {
            "gate_type": "prd-admission",
            "score": self.score,
            "threshold": self.threshold,
            "decision": self.decision.value,
            "passed": self.passed,
            "criteria": [criterion.__dict__ for criterion in self.criteria],
            "findings": [finding.__dict__ for finding in self.findings],
            "knowledge_request": {
                "required": self.knowledge_request.required,
                "reasons": list(self.knowledge_request.reasons),
                "expected_sources": list(self.knowledge_request.expected_sources),
            },
            "knowledge_snapshot": self.knowledge_snapshot.to_dict() if self.knowledge_snapshot else None,
        }


class LocalKnowledgeDocumentAdapter:
    """Read and normalize one document after PolicyEngine authorizes its path."""

    def execute(self, args: dict[str, object], workspace_root: Path) -> ToolExecutionOutput:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("read_document requires a relative path")
        path = workspace_root / raw_path
        content = path.read_bytes()
        suffix = path.suffix.lower()
        if suffix in ALLOWED_EXTENSIONS:
            document = ingest_upload(path.name, content)
            text = document.text
            content_hash = document.content_hash
        else:
            text = content.decode("utf-8-sig", errors="replace").strip()
            content_hash = f"sha256:{sha256(text.encode('utf-8')).hexdigest()}"
        return ToolExecutionOutput(
            exit_code=0,
            stdout=json.dumps(
                {"path": raw_path, "content_hash": content_hash, "text": text},
                ensure_ascii=False,
            ),
        )


def configured_knowledge_roots(value: str | None = None) -> tuple[Path, ...]:
    raw = os.environ.get("VAF_KNOWLEDGE_ROOTS", "") if value is None else value
    return tuple(Path(item).expanduser().resolve() for item in raw.split(os.pathsep) if item.strip())


def load_knowledge_base(
    path: str | Path,
    *,
    allowed_roots: tuple[Path, ...] | None = None,
) -> KnowledgeSnapshot:
    root = Path(path).expanduser().resolve()
    roots = configured_knowledge_roots() if allowed_roots is None else tuple(item.resolve() for item in allowed_roots)
    if not roots:
        raise KnowledgeBaseError("未配置 VAF_KNOWLEDGE_ROOTS，不能读取本地知识库目录")
    if not any(_is_within(root, allowed_root) for allowed_root in roots):
        raise KnowledgeBaseError("知识库目录不在 VAF_KNOWLEDGE_ROOTS 允许范围内")
    if not root.is_dir():
        raise KnowledgeBaseError("知识库目录不存在或不是目录")

    candidates = [
        item
        for item in sorted(root.rglob("*"))
        if item.is_file()
        and not item.is_symlink()
        and item.suffix.lower() in KNOWLEDGE_EXTENSIONS
        and not any(part.startswith(".") or part in {"node_modules", "dist", "build"} for part in item.relative_to(root).parts)
    ]
    if not candidates:
        raise KnowledgeBaseError("知识库中没有可读取的 Markdown、PDF、DOCX、HTML、TXT、CSV、JSON 或 YAML 文件")
    if len(candidates) > MAX_KNOWLEDGE_FILES:
        raise KnowledgeBaseError(f"知识库文件超过 {MAX_KNOWLEDGE_FILES} 个，请拆分后重试")
    total_bytes = sum(item.stat().st_size for item in candidates)
    if total_bytes > MAX_KNOWLEDGE_BYTES:
        raise KnowledgeBaseError("知识库文件总量超过 50 MB，请拆分后重试")

    gateway = ToolGateway(PolicyEngine(), {"read_document": LocalKnowledgeDocumentAdapter()})
    documents: list[KnowledgeDocument] = []
    for item in candidates:
        relative_path = item.relative_to(root).as_posix()
        result = gateway.execute(
            ToolRequest(
                tool_name="read_document",
                args={"path": relative_path},
                workspace_root=root,
                run_id="RUN-PRD-REVIEW",
                change_id="CHG-PRD-REVIEW",
                stage_run_id="prd-admission",
                idempotency_key=f"knowledge:{relative_path}:{item.stat().st_mtime_ns}:{item.stat().st_size}",
            )
        )
        if not result.output or result.output.exit_code != 0:
            raise KnowledgeBaseError(f"知识库文件读取失败：{relative_path}")
        payload = json.loads(result.output.stdout)
        text = str(payload.get("text", "")).strip()
        if text:
            documents.append(
                KnowledgeDocument(
                    path=relative_path,
                    content_hash=str(payload["content_hash"]),
                    text=text,
                )
            )
    if not documents:
        raise KnowledgeBaseError("知识库文件没有可用于评审的正文")
    digest_input = "\n".join(f"{item.path}:{item.content_hash}" for item in documents)
    return KnowledgeSnapshot(
        root=str(root),
        snapshot_hash=f"sha256:{sha256(digest_input.encode('utf-8')).hexdigest()}",
        documents=tuple(documents),
    )


def review_source_prd(
    document: IngestedDocument,
    knowledge_snapshot: KnowledgeSnapshot | None = None,
    *,
    threshold: float = 90.0,
) -> PrdReviewResult:
    """Review the user's source document before generating any downstream artifact."""

    text = document.text
    compact = re.sub(r"\s+", "", text)
    knowledge_request = detect_knowledge_request(text)
    criteria: list[PrdReviewCriterion] = []
    findings: list[PrdReviewFinding] = []

    _add_check(
        criteria,
        findings,
        "SOURCE",
        "来源可读性",
        5.0,
        len(compact) >= 200,
        True,
        f"提取到 {len(compact)} 个非空白字符",
        "PRD-SOURCE-001",
        "PRD 正文过短，无法建立可靠需求边界",
        "补充完整业务背景、范围、需求和验收条件",
    )
    _add_check(
        criteria,
        findings,
        "GOAL",
        "目标和业务价值",
        10.0,
        _contains(text, ("目标", "业务价值", "背景", "问题", "objective", "business value", "background")),
        True,
        "检测目标、背景或业务价值章节",
        "PRD-GOAL-001",
        "缺少明确目标或业务价值",
        "说明要解决的问题、目标用户和可衡量结果",
    )
    _add_check(
        criteria,
        findings,
        "USERS",
        "用户、角色和场景",
        5.0,
        _contains(text, ("用户", "角色", "用户故事", "使用场景", "流程", "user", "role", "scenario", "workflow")),
        False,
        "检测到用户、角色或业务场景",
        "PRD-USERS-001",
        "用户角色和使用场景不完整",
        "补充角色、入口、主流程和异常流程",
    )
    _add_check(
        criteria,
        findings,
        "SCOPE",
        "范围、非目标和约束",
        10.0,
        _contains(text, ("范围", "非目标", "不包含", "约束", "边界", "scope", "non-goal", "constraint", "out of scope")),
        True,
        "检测到范围、边界或非目标",
        "PRD-SCOPE-001",
        "需求范围和非目标不明确",
        "明确本期范围、非目标和不能突破的约束",
    )
    requirement_count = len(set(re.findall(r"\bREQ[-_ ]?\d+\b", text, re.IGNORECASE)))
    requirements_present = requirement_count > 0 or _contains(
        text,
        ("功能需求", "功能规格", "用户故事", "系统应", "必须支持", "shall", "functional requirement", "user story"),
    )
    criteria.append(
        PrdReviewCriterion(
            "REQUIREMENTS",
            "功能需求完整性",
            15.0,
            15.0 if requirement_count else 10.0 if requirements_present else 0.0,
            True,
            f"检测到 {requirement_count} 个显式 REQ ID"
            if requirement_count
            else "检测到功能需求行为，但没有稳定 REQ ID"
            if requirements_present
            else "没有检测到功能需求行为",
        )
    )
    if not requirements_present:
        findings.append(
            PrdReviewFinding(
                "PRD-REQ-001",
                "P0",
                "REQUIREMENTS",
                "缺少功能需求行为描述",
                "补充系统行为、触发条件和异常边界",
            )
        )
    elif not requirement_count:
        findings.append(
            PrdReviewFinding(
                "PRD-REQ-002",
                "P1",
                "REQUIREMENTS",
                "功能需求没有稳定 REQ ID，后续无法可靠追踪",
                "为每条系统行为分配稳定 REQ ID",
            )
        )
    acceptance_count = len(set(re.findall(r"\bAC[-_ ]?\d+\b", text, re.IGNORECASE)))
    acceptance_present = acceptance_count > 0 or _contains(
        text,
        ("验收", "预期结果", "given", "when", "then", "acceptance criteria"),
    )
    criteria.append(
        PrdReviewCriterion(
            "ACCEPTANCE",
            "验收条件可测试性",
            15.0,
            15.0 if acceptance_count else 10.0 if acceptance_present else 0.0,
            True,
            f"检测到 {acceptance_count} 个显式 AC ID"
            if acceptance_count
            else "检测到验收描述，但没有稳定 AC ID"
            if acceptance_present
            else "没有检测到可测试验收描述",
        )
    )
    if not acceptance_present:
        findings.append(
            PrdReviewFinding(
                "PRD-AC-001",
                "P0",
                "ACCEPTANCE",
                "缺少可观察、可测试的验收条件",
                "为每条关键需求补充输入、动作和可观察结果",
            )
        )
    elif not acceptance_count:
        findings.append(
            PrdReviewFinding(
                "PRD-AC-002",
                "P1",
                "ACCEPTANCE",
                "验收条件没有稳定 AC ID，后续无法可靠追踪",
                "为每条验收条件分配稳定 AC ID，并关联对应 REQ",
            )
        )
    _add_check(
        criteria,
        findings,
        "PROTOTYPE",
        "原型图片或页面截图",
        15.0,
        document.has_visual_evidence,
        True,
        "摄取层检测到视觉证据" if document.has_visual_evidence else "摄取层没有检测到视觉证据",
        "PRD-PROTOTYPE-001",
        "PRD 缺少原型图片、页面截图或线框图",
        "补充真实视觉原型；纯文字描述不能代替页面证据",
    )
    _add_check(
        criteria,
        findings,
        "DATA",
        "数据、接口和集成边界",
        5.0,
        _contains(text, ("数据", "字段", "接口", "api", "数据库", "集成", "webhook", "schema", "integration")),
        False,
        "检测到数据、接口或集成说明",
        "PRD-DATA-001",
        "数据和接口边界不完整",
        "补充核心实体、关键字段、外部接口和数据生命周期",
    )
    _add_check(
        criteria,
        findings,
        "NFR",
        "非功能、安全和隐私",
        10.0,
        _contains(text, ("非功能", "性能", "安全", "隐私", "权限", "可用性", "审计", "nfr", "security", "privacy", "performance")),
        False,
        "检测到非功能、安全或隐私要求",
        "PRD-NFR-001",
        "非功能、安全或隐私要求不完整",
        "补充性能、权限、隐私、审计、可用性和兼容性要求",
    )
    _add_check(
        criteria,
        findings,
        "RISKS",
        "风险、假设和待确认项",
        5.0,
        _contains(text, ("风险", "假设", "待确认", "依赖", "risk", "assumption", "open question", "dependency")),
        False,
        "检测到风险、假设或待确认项",
        "PRD-RISK-001",
        "风险、假设和待确认项没有显式记录",
        "列出未知事实、外部依赖、失败影响和确认责任",
    )

    knowledge_passed = not knowledge_request.required or _knowledge_is_relevant(text, knowledge_snapshot)
    knowledge_evidence = "当前 PRD 不依赖外部专有知识"
    if knowledge_request.required and knowledge_snapshot is None:
        knowledge_evidence = "检测到专有或外部事实引用，但没有知识库快照"
    elif knowledge_request.required and knowledge_snapshot is not None:
        knowledge_evidence = (
            f"知识库快照 {knowledge_snapshot.snapshot_hash} 与 PRD 存在领域关键词交集"
            if knowledge_passed
            else "知识库已读取，但没有找到与 PRD 风险信号相关的内容"
        )
    _add_check(
        criteria,
        findings,
        "KNOWLEDGE",
        "知识依据和来源完整性",
        5.0,
        knowledge_passed,
        knowledge_request.required,
        knowledge_evidence,
        "PRD-KB-001",
        "PRD 依赖未提供或不相关的本地知识资料",
        "提供业务术语、现有接口、数据字典、合规规则或既有系统文档目录",
    )

    score = round(sum(item.score for item in criteria), 2)
    if any(item.severity == "P0" for item in findings):
        decision = GateDecision.BLOCKED
    elif score > threshold:
        decision = GateDecision.PASS
    else:
        decision = GateDecision.NEEDS_CHANGES
    return PrdReviewResult(
        score=score,
        threshold=threshold,
        decision=decision,
        criteria=tuple(criteria),
        findings=tuple(findings),
        knowledge_request=knowledge_request,
        knowledge_snapshot=knowledge_snapshot,
    )


def detect_knowledge_request(text: str) -> KnowledgeRequest:
    groups = (
        (
            "需求引用了既有系统、内部规则或存量数据",
            r"现有系统|既有系统|内部规则|公司规范|历史数据|存量数据|存量数据库|legacy|existing system|internal policy",
            ("现有系统说明", "业务规则", "数据字典"),
        ),
        (
            "需求包含必须以真实契约为准的外部系统集成",
            r"第三方接口|外部接口|接口文档|对接.{0,16}(?:系统|平台|api)|sso|ldap|sap|erp|crm|payment gateway|webhook",
            ("OpenAPI/接口文档", "鉴权说明", "错误码和测试环境说明"),
        ),
        (
            "需求涉及受监管或高敏感业务规则",
            r"医疗|诊断|处方|证券|征信|保险理赔|反洗钱|实名核验|个人敏感信息|监管规则|合规要求",
            ("适用法规和合规规则", "数据分级规则", "领域术语表"),
        ),
        (
            "需求显式引用了未随 PRD 提供的其他资料",
            r"(?:参考|参照|按照|遵循|详见).{0,24}(?:文档|规范|规则|附件|知识库)",
            ("被引用的原始文档",),
        ),
    )
    reasons: list[str] = []
    sources: list[str] = []
    for reason, pattern, expected_sources in groups:
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
            reasons.append(reason)
            sources.extend(expected_sources)
    return KnowledgeRequest(bool(reasons), tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(sources)))


def _knowledge_is_relevant(prd_text: str, snapshot: KnowledgeSnapshot | None) -> bool:
    if snapshot is None:
        return False
    signal_terms = _knowledge_terms(prd_text)
    knowledge_terms = _knowledge_terms(snapshot.text)
    return len(signal_terms.intersection(knowledge_terms)) >= 3


def _knowledge_terms(text: str) -> set[str]:
    terms = {
        term.lower()
        for term in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text)
        if term.lower() not in _STOP_TERMS
    }
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for size in (2, 3, 4):
            terms.update(sequence[index:index + size] for index in range(max(0, len(sequence) - size + 1)))
    return {term for term in terms if term not in _STOP_TERMS}


def _add_check(
    criteria: list[PrdReviewCriterion],
    findings: list[PrdReviewFinding],
    criterion_id: str,
    label: str,
    weight: float,
    passed: bool,
    blocking: bool,
    success_evidence: str,
    finding_id: str,
    message: str,
    correction: str,
) -> None:
    criteria.append(
        PrdReviewCriterion(
            criterion_id=criterion_id,
            label=label,
            weight=weight,
            score=weight if passed else 0.0,
            blocking=blocking,
            evidence=success_evidence,
        )
    )
    if not passed:
        findings.append(
            PrdReviewFinding(
                finding_id=finding_id,
                severity="P0" if blocking else "P1",
                criterion_id=criterion_id,
                message=message,
                correction=correction,
            )
        )


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    normalized = text.lower()
    return any(term.lower() in normalized for term in terms)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


_STOP_TERMS = {
    "prd",
    "需求",
    "系统",
    "功能",
    "用户",
    "支持",
    "实现",
    "页面",
    "数据",
    "接口",
    "业务",
    "进行",
    "需要",
    "可以",
    "以及",
    "the",
    "and",
    "with",
    "shall",
}
