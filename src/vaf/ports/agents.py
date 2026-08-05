"""Agent contracts used by the application layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


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
    def generate_code(
        self,
        *,
        change_id: str,
        title: str,
        objective: str,
        implementation: Mapping[str, object],
    ) -> CodeGenerationResult:
        """Generate candidate file changes from an approved implementation plan."""
