"""Agent contracts used by the application layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class ImageInput:
    """A bounded image supplied to a multimodal agent."""

    source_name: str
    content_hash: str
    media_type: str | None = None
    data: bytes | None = None
    url: str | None = None


@dataclass(frozen=True)
class DraftResult:
    artifact_type: str
    content: str
    assumptions: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CodeChange:
    task_id: str
    path: str
    content: str
    requirement_ids: tuple[str, ...] = ()
    acceptance_ids: tuple[str, ...] = ()
    test_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CodeGenerationResult:
    changes: tuple[CodeChange, ...]
    assumptions: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()


class AgentPort(Protocol):
    def draft_prd(
        self,
        change_id: str,
        title: str,
        objective: str,
        version: int = 1,
    ) -> DraftResult:
        """Generate a versioned PRD candidate."""

    def draft_artifact(
        self,
        artifact_type: str,
        change_id: str,
        title: str,
        objective: str,
        version: int = 1,
    ) -> DraftResult:
        """Generate a technical design, test case set, or implementation plan."""

    def generate_code(
        self,
        *,
        change_id: str,
        title: str,
        objective: str,
        implementation: Mapping[str, object],
    ) -> CodeGenerationResult:
        """Generate candidate file changes from an approved implementation plan."""
