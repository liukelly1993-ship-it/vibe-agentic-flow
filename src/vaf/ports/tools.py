"""Tool adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ToolExecutionOutput:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ToolAdapter(Protocol):
    def execute(self, args: dict[str, object], workspace_root: Path) -> ToolExecutionOutput:
        """Execute an already authorized request."""
