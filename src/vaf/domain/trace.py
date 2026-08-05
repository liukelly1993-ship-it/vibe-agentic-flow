"""TraceLink validation and coverage calculations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TraceRelation(StrEnum):
    DERIVED_FROM = "derived_from"
    SATISFIES = "satisfies"
    IMPLEMENTS = "implements"
    VERIFIES = "verifies"
    EVIDENCED_BY = "evidenced_by"
    INVALIDATED_BY = "invalidated_by"


@dataclass(frozen=True)
class TraceLink:
    link_id: str
    from_ref: str
    relation: TraceRelation
    to_ref: str
    from_hash: str
    to_hash: str
    status: str = "active"


@dataclass(frozen=True)
class CoverageReport:
    acceptance_total: int
    acceptance_covered: int
    code_files_total: int
    code_files_explained: int

    @property
    def acceptance_ratio(self) -> float:
        return self.acceptance_covered / self.acceptance_total if self.acceptance_total else 1.0

    @property
    def code_explainability_ratio(self) -> float:
        return self.code_files_explained / self.code_files_total if self.code_files_total else 1.0

    @property
    def passed(self) -> bool:
        return self.acceptance_ratio == 1.0 and self.code_explainability_ratio == 1.0


def validate_links(links: list[TraceLink], valid_refs: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for link in links:
        for ref, content_hash in ((link.from_ref, link.from_hash), (link.to_ref, link.to_hash)):
            if ref not in valid_refs:
                errors.append(f"missing trace object: {ref}")
            elif valid_refs[ref] != content_hash:
                errors.append(f"stale trace hash: {ref}")
        if link.status != "active":
            continue
        if not link.from_ref or not link.to_ref:
            errors.append(f"empty trace endpoint: {link.link_id}")
    return errors


def calculate_coverage(
    acceptance_ids: set[str],
    passed_test_ids: set[str],
    links: list[TraceLink],
    code_file_refs: set[str],
) -> CoverageReport:
    covered_acceptance = {
        link.from_ref
        for link in links
        if link.relation == TraceRelation.VERIFIES
        and link.from_ref in acceptance_ids
        and link.to_ref in passed_test_ids
        and link.status == "active"
    }
    explained_files = {
        link.from_ref
        for link in links
        if link.from_ref in code_file_refs
        and link.relation == TraceRelation.IMPLEMENTS
        and link.status == "active"
        and any(
            other.from_ref == link.to_ref
            and other.relation == TraceRelation.SATISFIES
            and other.status == "active"
            for other in links
        )
    }
    return CoverageReport(
        acceptance_total=len(acceptance_ids),
        acceptance_covered=len(covered_acceptance),
        code_files_total=len(code_file_refs),
        code_files_explained=len(explained_files),
    )
