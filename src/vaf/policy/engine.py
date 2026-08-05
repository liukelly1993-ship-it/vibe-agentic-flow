"""A minimal, enforceable Policy Gateway for v0.1."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PolicyDecisionType(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


@dataclass(frozen=True)
class ToolRequest:
    tool_name: str
    args: dict[str, object]
    workspace_root: Path
    network_mode: str = "disabled"
    profile: str = "standard"
    run_id: str = "RUN-LOCAL"
    change_id: str = "CHG-LOCAL"
    stage_run_id: str | None = None
    actor: str = "system"
    idempotency_key: str | None = None


@dataclass(frozen=True)
class PolicyDecision:
    decision_id: str
    decision: PolicyDecisionType
    rule_id: str
    reason: str
    policy_version: str = "v0.1"


class PolicyEngine:
    """Evaluate tool requests before any adapter is invoked."""

    def __init__(self, allowed_commands: set[tuple[str, ...]] | None = None) -> None:
        self.allowed_commands = allowed_commands or {
            ("ruff", "format", "--check", "."),
            ("ruff", "check", "."),
            ("pytest", "-q"),
            ("python", "-m", "unittest"),
            ("python", "-m", "unittest", "discover", "-s", "tests", "-t", "."),
        }
        self._counter = 0

    def evaluate(self, request: ToolRequest) -> PolicyDecision:
        self._counter += 1
        decision_id = f"POL-{self._counter:06d}"
        if request.tool_name in {"network_request", "read_secret", "deploy_production"}:
            return PolicyDecision(
                decision_id, PolicyDecisionType.DENY, "VAF-POLICY-DEFAULT-DENY",
                f"tool is not available in v0.1: {request.tool_name}",
            )
        if request.network_mode != "disabled":
            return PolicyDecision(
                decision_id, PolicyDecisionType.DENY, "VAF-POLICY-NETWORK",
                "network must be disabled for v0.1 tool execution",
            )
        if request.tool_name == "git_worktree_add":
            if not isinstance(request.args.get("change_id"), str) or not isinstance(request.args.get("run_id"), str):
                return PolicyDecision(
                    decision_id,
                    PolicyDecisionType.DENY,
                    "VAF-POLICY-WORKTREE",
                    "worktree creation requires system-generated change_id and run_id",
                )
            if request.args.get("base_ref", "HEAD") != "HEAD":
                return PolicyDecision(
                    decision_id,
                    PolicyDecisionType.DENY,
                    "VAF-POLICY-WORKTREE",
                    "v0.1 worktrees must use HEAD as the base ref",
                )
            return PolicyDecision(decision_id, PolicyDecisionType.ALLOW, "VAF-POLICY-WORKTREE", "worktree request is constrained")
        if request.tool_name in {"write_file", "run_command"}:
            path = request.args.get("path")
            if path is not None and not self._inside_workspace(request.workspace_root, Path(str(path))):
                return PolicyDecision(
                    decision_id, PolicyDecisionType.DENY, "VAF-POLICY-PATH",
                    "path is outside the isolated workspace",
                )
        if request.tool_name == "run_command":
            argv = request.args.get("argv")
            normalized = tuple(str(item) for item in argv) if isinstance(argv, (list, tuple)) else ()
            if normalized not in self.allowed_commands:
                return PolicyDecision(
                    decision_id, PolicyDecisionType.DENY, "VAF-POLICY-COMMAND",
                    "command is not in the project allowlist",
                )
        if request.tool_name == "write_file" and request.profile == "enterprise":
            return PolicyDecision(
                decision_id, PolicyDecisionType.REQUIRE_APPROVAL, "VAF-POLICY-WRITE-APPROVAL",
                "enterprise profile requires approval for file writes",
            )
        return PolicyDecision(decision_id, PolicyDecisionType.ALLOW, "VAF-POLICY-ALLOW", "request satisfies v0.1 rules")

    @staticmethod
    def _inside_workspace(workspace_root: Path, requested: Path) -> bool:
        root = workspace_root.resolve()
        candidate = requested if requested.is_absolute() else root / requested
        try:
            candidate.resolve().relative_to(root)
        except ValueError:
            return False
        return True
