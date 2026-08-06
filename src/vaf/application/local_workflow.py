"""Local CLI workflow for the first VAF vertical slice."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

import yaml

from vaf.adapters.git_worktree import GitWorktreeManager, GitWorktreeToolAdapter, WorktreeHandle
from vaf.adapters.jsonl_event_store import JsonlEventStore
from vaf.adapters.tool_gateway import LocalCommandAdapter, LocalFileAdapter, ToolGateway
from vaf.agents.fake_agent import DraftResult, FakeAgent
from vaf.application.run_projection import RunState, project_events
from vaf.domain.artifacts import ArtifactVersion, split_frontmatter
from vaf.domain.events import EventEnvelope
from vaf.domain.gates import evaluate_artifact_gate, evaluate_code_gate, validate_domain_contract
from vaf.domain.ids import new_id
from vaf.domain.states import StageCommand, StageStatus, TransitionError, transition
from vaf.domain.trace import TraceLink, TraceRelation, calculate_coverage, validate_links
from vaf.policy.engine import PolicyEngine, ToolRequest
from vaf.ports.agents import AgentPort


class WorkflowError(RuntimeError):
    """Raised for user-correctable workflow errors."""


class LocalWorkflow:
    def __init__(self, project_root: str | Path, agent: AgentPort | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.vaf_root = self.project_root / ".vaf"
        self.agent = agent or FakeAgent()

    def init_project(self) -> list[Path]:
        self._require_git_repo()
        created: list[Path] = []
        directories = [
            self.vaf_root,
            self.vaf_root / "changes",
            self.vaf_root / "artifacts",
            self.vaf_root / "traces",
            self.vaf_root / "workflows",
            self.vaf_root / "runs",
            self.vaf_root / "locks",
        ]
        for directory in directories:
            if not directory.exists():
                directory.mkdir(parents=True)
                created.append(directory)
        files = {
            self.vaf_root / "manifest.yaml": {
                "vaf_version": "0.1",
                "project_root": str(self.project_root),
                "profile": "standard",
                "verification": {
                    "default_command": "unit-test",
                    "commands": [
                        {
                            "id": "unit-test",
                            "argv": ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                            "network": "disabled",
                        }
                    ]
                },
            },
            self.vaf_root / "constitution.md": "# VAF 项目规则\n\n- 只在隔离 worktree 中修改代码。\n- 所有验证命令必须经过 Policy Gateway。\n",
            self.vaf_root / "workflows" / "default.yaml": {
                "workflow_version": "v0.1",
                "stages": ["prd", "technical-design", "test-cases", "implementation-plan"],
            },
        }
        for path, value in files.items():
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, str):
                path.write_text(value, encoding="utf-8")
            else:
                path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
            created.append(path)
        return created

    def run(
        self,
        change_id: str,
        title: str,
        objective: str,
        source: str = "cli",
        implementation_spec: str | Path | None = None,
    ) -> RunState:
        self.init_project()
        change = self._load_or_create_change(change_id, title, objective, source, implementation_spec)
        run_id = new_id("RUN")
        run_dir = self.vaf_root / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        store = JsonlEventStore(run_dir / "events.jsonl")
        store.append(
            EventEnvelope.create(
                event_type="RunStarted",
                run_id=run_id,
                change_id=change_id,
                payload={"title": change["title"], "stage": "prd"},
            )
        )
        store.append(
            EventEnvelope.create(
                event_type="StageStarted",
                run_id=run_id,
                change_id=change_id,
                payload={"stage": "prd", "attempt": 1},
            )
        )
        draft = self.agent.draft_prd(change_id, title, objective)
        artifact = self._write_artifact(change_id, draft, version=1)
        store.append(
            EventEnvelope.create(
                event_type="ArtifactDrafted",
                run_id=run_id,
                change_id=change_id,
                payload=artifact,
            )
        )
        self._write_run_index(run_id, change_id)
        return self.state(run_id)

    def autopilot(
        self,
        change_id: str,
        title: str,
        objective: str,
        source: str = "autopilot",
        implementation_spec: str | Path | None = None,
        max_attempts: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Run the score-gated workflow without waiting for human approval.

        ``max_attempts`` is an optional diagnostic cap for tests or bounded CLI
        runs. The Web control plane leaves it unset and does not stop a normal
        score-repair loop because an arbitrary retry count was reached.
        """

        if max_attempts is not None and max_attempts < 1:
            raise WorkflowError("VAF-AUTOPILOT-001: max_attempts must be >= 1")
        state = self.run(change_id, title, objective, source, implementation_spec)
        gate_attempts: dict[str, int] = {}
        last_gate_error = ""
        steps = 0
        max_steps = max_attempts * 10 + 10 if max_attempts is not None else None

        def notify(extra: dict[str, Any] | None = None) -> None:
            if progress_callback is None:
                return
            payload: dict[str, Any] = {
                "run_id": state.run_id,
                "step": steps,
                "status": state.status,
                "stage": state.current_stage,
                "attempt": gate_attempts.get(state.current_stage or "", 0),
            }
            if extra:
                payload.update(extra)
            try:
                progress_callback(payload)
            except Exception:
                # Progress reporting cannot change workflow correctness.
                pass

        while max_steps is None or steps < max_steps:
            steps += 1
            notify()
            if state.status == "WAITING_REVIEW":
                stage = state.current_stage or "unknown"
                gate_attempts[stage] = gate_attempts.get(stage, 0) + 1
                if max_attempts is not None and gate_attempts[stage] > max_attempts:
                    raise WorkflowError(
                        f"VAF-AUTOPILOT-004: gate retry budget exhausted at stage {stage}; "
                        f"{last_gate_error or 'no gate result'}"
                    )
                try:
                    state = self.approve(
                        state.run_id,
                        "gate-engine",
                        state.artifact_hash or "",
                        "automatic score gate passed",
                    )
                except WorkflowError as exc:
                    last_gate_error = str(exc)
                    state = self.state(state.run_id)
                    notify({"error": last_gate_error})
                    if state.status != "CHANGES_REQUESTED":
                        raise
                continue
            if state.status in {"CHANGES_REQUESTED", "APPROVED"}:
                state = self.resume(state.run_id)
                continue
            if state.status == "READY_FOR_IMPLEMENTATION":
                state = self.implement(state.run_id)
                continue
            if state.status == "IMPLEMENTED":
                state = self.verify(state.run_id)
                continue
            if state.status in {"VERIFIED", "FAILED"}:
                trace = self.trace(state.run_id)
                if trace["status"] == "passed":
                    return {"state": self.state(state.run_id).__dict__, "trace": trace}
                raise WorkflowError(
                    f"VAF-AUTOPILOT-002: quality gate did not pass; {trace['quality_gate']['decision']} "
                    f"score={trace['quality_gate']['score']:.2f}"
                )
            raise WorkflowError(f"VAF-AUTOPILOT-003: unsupported workflow status: {state.status}")
        raise WorkflowError(f"VAF-AUTOPILOT-005: diagnostic workflow step budget exhausted; {last_gate_error or 'no gate result'}")

    def state(self, run_id: str) -> RunState:
        store = self._store(run_id)
        try:
            store.verify_chain()
            return project_events(store.read_all())
        except ValueError as exc:
            raise WorkflowError(f"invalid event stream for run {run_id}: {exc}") from exc

    def review(self, run_id: str) -> dict[str, Any]:
        state = self.state(run_id)
        if not state.artifact_path:
            raise WorkflowError("run has no reviewable artifact")
        content = Path(state.artifact_path).read_text(encoding="utf-8")
        metadata, body = split_frontmatter(content)
        artifact = ArtifactVersion.from_markdown(content)
        gate = evaluate_artifact_gate(artifact.artifact_type.value, content, target_hash=artifact.content_hash)
        return {"state": state.__dict__, "metadata": _json_safe(metadata), "body": body, "gate": gate.to_dict()}

    def approve(self, run_id: str, actor: str, target_hash: str, comment: str = "") -> RunState:
        state = self.state(run_id)
        self._require_status(state, "WAITING_REVIEW")
        self._require_transition(state, StageCommand.APPROVE)
        source = self._review_source(state, target_hash)
        content = source.read_text(encoding="utf-8")
        current_artifact = ArtifactVersion.from_markdown(content)
        if current_artifact.content_hash != target_hash:
            raise WorkflowError("VAF-APPROVAL-STALE: artifact changed after review; review it again")
        gate = evaluate_artifact_gate(current_artifact.artifact_type.value, content, target_hash=target_hash)
        self._append_event(
            run_id,
            EventEnvelope.create(
                event_type="GateEvaluated",
                run_id=run_id,
                change_id=state.change_id,
                actor="system",
                payload=gate.to_dict(),
            ),
        )
        if not gate.passed:
            finding = gate.findings[0].message if gate.findings else "score is not above the gate threshold"
            event_type = "GateBlocked" if gate.decision.value == "BLOCKED" else "ArtifactChangesRequested"
            self._append_event(
                run_id,
                EventEnvelope.create(
                    event_type=event_type,
                    run_id=run_id,
                    change_id=state.change_id,
                    actor="system",
                    payload={
                        "artifact_id": state.artifact_id,
                        "target_hash": target_hash,
                        "comment": f"Gate rejected: {finding}",
                        "correction_target_stage": state.current_stage,
                        "gate_decision": gate.decision.value,
                        "gate_score": gate.score,
                        "recovery_condition": "补充新的可信输入后重新运行" if gate.decision.value == "BLOCKED" else "重新生成当前阶段产物",
                    },
                ),
            )
            self._write_run_index(run_id, state.change_id)
            raise WorkflowError(
                f"VAF-GATE-001: {gate.decision.value}, score={gate.score:.2f}, threshold>{gate.threshold:.2f}; {finding}"
            )
        metadata, body = split_frontmatter(content)
        metadata["version"] = int(metadata["version"]) + 1
        metadata["status"] = "approved"
        metadata["approved_by"] = actor
        metadata["approved_at"] = datetime.now(timezone.utc).isoformat()
        content = _render_artifact(metadata, body)
        artifact = self._write_versioned_content(state.change_id, str(metadata["artifact_type"]), content, int(metadata["version"]))
        self._append_event(
            run_id,
            EventEnvelope.create(
                event_type="ArtifactApproved",
                run_id=run_id,
                change_id=state.change_id,
                actor=actor,
                payload={**artifact, "comment": comment},
            )
        )
        self._write_run_index(run_id, state.change_id)
        return self.state(run_id)

    def reject(self, run_id: str, actor: str, target_hash: str, comment: str) -> RunState:
        if not comment.strip():
            raise WorkflowError("reject requires a non-empty comment")
        state = self.state(run_id)
        self._require_status(state, "WAITING_REVIEW")
        self._require_transition(state, StageCommand.REQUEST_CHANGES)
        self._review_source(state, target_hash)
        self._append_event(
            run_id,
            EventEnvelope.create(
                event_type="ArtifactChangesRequested",
                run_id=run_id,
                change_id=state.change_id,
                actor=actor,
                payload={"artifact_id": state.artifact_id, "target_hash": target_hash, "comment": comment},
            )
        )
        self._write_run_index(run_id, state.change_id)
        return self.state(run_id)

    def resume(self, run_id: str) -> RunState:
        state = self.state(run_id)
        if state.status == "CHANGES_REQUESTED":
            self._require_transition(state, StageCommand.REGENERATE)
            artifact_type = state.current_stage or "prd"
            version = (state.artifact_version or 1) + 1
        elif state.status == "APPROVED":
            self._require_transition(state, StageCommand.ADVANCE)
            next_stage = {"prd": "technical-design", "technical-design": "test-cases", "test-cases": "implementation-plan"}.get(state.current_stage or "")
            if next_stage is None:
                self._append_event(
                    run_id,
                    EventEnvelope.create(
                        event_type="RunCompleted",
                        run_id=run_id,
                        change_id=state.change_id,
                        payload={"status": "READY_FOR_IMPLEMENTATION"},
                    )
                )
                self._write_run_index(run_id, state.change_id)
                return self.state(run_id)
            artifact_type = next_stage
            version = 1
        else:
            raise WorkflowError(f"run cannot resume from status: {state.status}")
        change = self._load_change(state.change_id)
        self._append_event(
            run_id,
            EventEnvelope.create(
                event_type="StageStarted",
                run_id=run_id,
                change_id=state.change_id,
                payload={"stage": artifact_type, "attempt": version},
            )
        )
        draft = self.agent.draft_artifact(artifact_type, state.change_id, change["title"], change["objective"], version)
        artifact = self._write_artifact(state.change_id, draft, version)
        self._append_event(
            run_id,
            EventEnvelope.create(
                event_type="ArtifactDrafted",
                run_id=run_id,
                change_id=state.change_id,
                payload=artifact,
            )
        )
        self._write_run_index(run_id, state.change_id)
        return self.state(run_id)

    def implement(self, run_id: str) -> RunState:
        state = self.state(run_id)
        change = self._load_change(state.change_id)
        implementation = change.get("implementation")
        if not isinstance(implementation, dict):
            raise WorkflowError(
                "change has no implementation plan; provide implementation.changes in the Change YAML"
            )

        manager = GitWorktreeManager(self.project_root)
        if state.status == "READY_FOR_IMPLEMENTATION":
            creation_gateway = ToolGateway(
                PolicyEngine(),
                {"git_worktree_add": GitWorktreeToolAdapter(manager)},
                self._store(run_id),
            )
            creation = creation_gateway.execute(
                ToolRequest(
                    tool_name="git_worktree_add",
                    args={"change_id": state.change_id, "run_id": run_id, "base_ref": "HEAD"},
                    workspace_root=self.project_root,
                    run_id=run_id,
                    change_id=state.change_id,
                    stage_run_id="implementation",
                    idempotency_key=f"worktree:{run_id}:HEAD",
                )
            )
            if not creation.output or creation.output.exit_code != 0:
                raise WorkflowError(
                    f"failed to create worktree: {creation.error_code or 'unknown error'}"
                )
            try:
                worktree_data = json.loads(creation.output.stdout)
                handle = WorktreeHandle(
                    change_id=str(worktree_data["change_id"]),
                    run_id=str(worktree_data["run_id"]),
                    path=Path(str(worktree_data["path"])),
                    branch=str(worktree_data["branch"]),
                    lock_path=Path(str(worktree_data["lock_path"])),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise WorkflowError("VAF-WORKTREE-001: invalid worktree creation result") from exc
            self._append_event(
                run_id,
                EventEnvelope.create(
                    event_type="StageStarted",
                    run_id=run_id,
                    change_id=state.change_id,
                    stage_run_id="implementation",
                    payload={"stage": "implementation", "attempt": 1},
                ),
            )
            self._append_event(
                run_id,
                EventEnvelope.create(
                    event_type="WorktreeCreated",
                    run_id=run_id,
                    change_id=state.change_id,
                    stage_run_id="implementation",
                    payload={"path": str(handle.path), "branch": handle.branch},
                ),
            )
        elif state.status == "IMPLEMENTING" and state.worktree_path and state.worktree_branch:
            handle = WorktreeHandle(
                change_id=state.change_id,
                run_id=run_id,
                path=Path(state.worktree_path),
                branch=state.worktree_branch,
                lock_path=manager.lock_base / f"{state.change_id}.lock",
            )
            if not handle.path.is_dir():
                raise WorkflowError(f"recorded implementation worktree is missing: {handle.path}")
        else:
            raise WorkflowError(f"run cannot implement from status: {state.status}")
        try:
            generated = self.agent.generate_code(
                change_id=state.change_id,
                title=str(change["title"]),
                objective=str(change["objective"]),
                implementation=implementation,
            )
            allowed_paths = {change_file.path for change_file in generated.changes}
            gateway = ToolGateway(
                PolicyEngine(),
                {"write_file": LocalFileAdapter()},
                self._store(run_id),
            )
            already_written = set(self.state(run_id).changed_paths)
            for change_file in generated.changes:
                result = gateway.execute(
                    ToolRequest(
                        tool_name="write_file",
                        args={"path": change_file.path, "content": change_file.content},
                        workspace_root=handle.path,
                        run_id=run_id,
                        change_id=state.change_id,
                        stage_run_id="implementation",
                        idempotency_key=(
                            f"write:{run_id}:{change_file.path}:"
                            f"{hashlib.sha256(change_file.content.encode('utf-8')).hexdigest()}"
                        ),
                    )
                )
                if not result.output or result.output.exit_code != 0:
                    raise WorkflowError(
                        f"failed to write {change_file.path}: {result.error_code or 'unknown error'}"
                    )
                manager.assert_allowed_changes(handle, allowed_paths)
                if change_file.path not in already_written:
                    self._append_event(
                        run_id,
                        EventEnvelope.create(
                            event_type="CodeFileWritten",
                            run_id=run_id,
                            change_id=state.change_id,
                            stage_run_id="implementation",
                            payload={
                                "task_id": change_file.task_id,
                                "path": change_file.path,
                                "requirement_ids": list(change_file.requirement_ids),
                                "acceptance_ids": list(change_file.acceptance_ids),
                                "test_ids": list(change_file.test_ids),
                            },
                        ),
                    )
                    already_written.add(change_file.path)
            changed_paths = manager.changed_paths(handle)
            manager.assert_allowed_changes(handle, allowed_paths)
            self._append_event(
                run_id,
                EventEnvelope.create(
                    event_type="ImplementationCompleted",
                    run_id=run_id,
                    change_id=state.change_id,
                    stage_run_id="implementation",
                    payload={"changed_paths": list(changed_paths)},
                ),
            )
        except Exception as exc:
            self._append_event(
                run_id,
                EventEnvelope.create(
                    event_type="ImplementationFailed",
                    run_id=run_id,
                    change_id=state.change_id,
                    stage_run_id="implementation",
                    payload={"error": str(exc)},
                ),
            )
            if isinstance(exc, WorkflowError):
                raise
            raise WorkflowError(str(exc)) from exc
        return self.state(run_id)

    def verify(self, run_id: str) -> RunState:
        state = self.state(run_id)
        events = self._store(run_id).read_all()
        if state.current_stage != "implementation" or not any(
            event.event_type == "ImplementationCompleted" for event in events
        ):
            raise WorkflowError(
                "VAF-STATE-001: verification requires a completed implementation stage"
            )
        store = self._store(run_id)
        workspace_root = Path(state.worktree_path) if state.worktree_path else self.project_root
        workspace_fingerprint = GitWorktreeManager.workspace_fingerprint(workspace_root)
        command_id, argv, network_mode, command_spec_hash = self._load_verification_command()
        request = ToolRequest(
            tool_name="run_command",
            args={"argv": list(argv)},
            workspace_root=workspace_root,
            network_mode=network_mode,
            run_id=run_id,
            change_id=state.change_id,
            stage_run_id=state.current_stage,
            idempotency_key=f"verify:{state.current_stage}:{workspace_fingerprint}:{command_spec_hash}",
        )
        gateway = ToolGateway(PolicyEngine(), {"run_command": LocalCommandAdapter()}, store)
        result = gateway.execute(request)
        self._append_event(
            run_id,
            EventEnvelope.create(
                event_type="VerificationCompleted",
                run_id=run_id,
                change_id=state.change_id,
                payload={
                    "invocation_id": result.invocation_id,
                    "command_id": command_id,
                    "argv": list(argv),
                    "command_spec_hash": command_spec_hash,
                    "exit_code": result.output.exit_code if result.output else None,
                    "error_code": result.error_code,
                    "workspace_fingerprint": workspace_fingerprint,
                    "stdout": _truncate(result.output.stdout if result.output else ""),
                    "stderr": _truncate(result.output.stderr if result.output else ""),
                },
            )
        )
        self._write_run_index(run_id, state.change_id)
        return self.state(run_id)

    def trace(self, run_id: str) -> dict[str, Any]:
        state = self.state(run_id)
        events = self._store(run_id).read_all()
        artifacts = [event.payload for event in events if event.event_type in {"ArtifactDrafted", "ArtifactApproved"}]
        verification = [event.payload for event in events if event.event_type == "VerificationCompleted"]
        approved_artifacts = {
            str(event.payload["artifact_type"]): event.payload
            for event in events
            if event.event_type == "ArtifactApproved" and event.payload.get("artifact_type")
        }
        valid_refs: dict[str, str] = {}
        for payload in approved_artifacts.values():
            artifact_path = Path(str(payload["path"]))
            try:
                content = artifact_path.read_text(encoding="utf-8")
            except OSError:
                continue
            for ref in set(re.findall(r"\b(?:REQ|AC|TC|TASK)-\d+\b", content)):
                valid_refs[ref] = str(payload["content_hash"])

        links: list[TraceLink] = []
        link_keys: set[tuple[str, str, str]] = set()

        def add_link(
            from_ref: str,
            relation: TraceRelation,
            to_ref: str,
            from_hash: str | None,
            to_hash: str | None,
        ) -> None:
            key = (from_ref, relation.value, to_ref)
            if key in link_keys:
                return
            link_keys.add(key)
            links.append(
                TraceLink(
                    link_id=f"LINK-{len(links) + 1:04d}",
                    from_ref=from_ref,
                    relation=relation,
                    to_ref=to_ref,
                    from_hash=from_hash or "",
                    to_hash=to_hash or "",
                )
            )

        code_events = [event for event in events if event.event_type == "CodeFileWritten"]
        code_file_refs: set[str] = set()
        for event in code_events:
            payload = event.payload
            relative_path = str(payload["path"])
            file_ref = f"FILE:{relative_path}"
            file_hash = _hash_workspace_file(state.worktree_path, relative_path)
            if file_hash:
                valid_refs[file_ref] = file_hash
            task_ref = str(payload["task_id"])
            task_hash = valid_refs.get(task_ref)
            add_link(file_ref, TraceRelation.IMPLEMENTS, task_ref, file_hash, task_hash)
            for requirement_id in payload.get("requirement_ids", []):
                requirement_ref = str(requirement_id)
                add_link(task_ref, TraceRelation.SATISFIES, requirement_ref, task_hash, valid_refs.get(requirement_ref))
            for acceptance_id in payload.get("acceptance_ids", []):
                acceptance_ref = str(acceptance_id)
                for test_id in payload.get("test_ids", []):
                    test_ref = str(test_id)
                    add_link(acceptance_ref, TraceRelation.VERIFIES, test_ref, valid_refs.get(acceptance_ref), valid_refs.get(test_ref))
            if not relative_path.startswith("tests/") and not relative_path.endswith(".pyc"):
                code_file_refs.add(file_ref)

        latest_verification = verification[-1] if verification else None
        current_command_spec_hash: str | None = None
        manifest_error: str | None = None
        try:
            _command_id, _argv, _network_mode, current_command_spec_hash = self._load_verification_command()
        except WorkflowError as exc:
            manifest_error = str(exc)
        current_workspace_fingerprint = (
            GitWorktreeManager.workspace_fingerprint(state.worktree_path)
            if state.worktree_path
            else None
        )
        verification_matches_workspace = bool(
            latest_verification
            and current_workspace_fingerprint
            and latest_verification.get("workspace_fingerprint") == current_workspace_fingerprint
        )
        verification_matches_command = bool(
            latest_verification
            and current_command_spec_hash
            and latest_verification.get("command_spec_hash") == current_command_spec_hash
        )
        passed_test_ids = {
            str(test_id)
            for event in code_events
            for test_id in event.payload.get("test_ids", [])
        } if verification_matches_workspace and verification_matches_command and latest_verification.get("exit_code") == 0 else set()
        acceptance_ids = {ref for ref in valid_refs if ref.startswith("AC-")}
        coverage = calculate_coverage(acceptance_ids, passed_test_ids, links, code_file_refs)
        validation_errors = validate_links(links, valid_refs)
        if manifest_error:
            validation_errors.append(manifest_error)
        prd_payload = approved_artifacts.get("prd")
        prd_content = ""
        if prd_payload:
            try:
                prd_content = Path(str(prd_payload["path"])).read_text(encoding="utf-8")
            except (KeyError, OSError, UnicodeDecodeError):
                validation_errors.append("approved PRD evidence is unreadable")
        validation_errors.extend(
            validate_domain_contract(prd_content, state.worktree_path, tuple(state.changed_paths))
        )
        change = self._load_change(state.change_id)
        implementation = change.get("implementation")
        declared_paths: set[str] = set()
        planned_lines = 0
        if isinstance(implementation, dict) and isinstance(implementation.get("changes"), list):
            for item in implementation["changes"]:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    declared_paths.add(str(item["path"]))
                    if isinstance(item.get("content"), str):
                        planned_lines += len(str(item["content"]).splitlines())
        verification_exit_code = (
            latest_verification.get("exit_code")
            if verification_matches_workspace and verification_matches_command and latest_verification
            else None
        )
        quality_gate = evaluate_code_gate(
            worktree_path=state.worktree_path,
            changed_paths=tuple(state.changed_paths),
            declared_paths=declared_paths,
            planned_lines=planned_lines,
            acceptance_ratio=coverage.acceptance_ratio,
            code_explainability_ratio=coverage.code_explainability_ratio,
            validation_errors=validation_errors,
            verification_exit_code=verification_exit_code,
            target_hash=current_workspace_fingerprint,
        )
        quality_passed = (
            bool(code_events)
            and verification_matches_workspace
            and verification_matches_command
            and latest_verification.get("exit_code") == 0
            and coverage.passed
            and not validation_errors
            and quality_gate.passed
        )
        report = {
            "run_id": run_id,
            "change_id": state.change_id,
            "status": "passed" if quality_passed else "incomplete",
            "reason": "TraceLink and verification quality gates passed" if quality_passed else "trace links or verification evidence are incomplete",
            "artifacts": artifacts,
            "implementation": {
                "worktree_path": state.worktree_path,
                "worktree_branch": state.worktree_branch,
                "changed_paths": list(state.changed_paths),
            },
            "verification": latest_verification,
            "verification_matches_workspace": verification_matches_workspace,
            "verification_matches_command": verification_matches_command,
            "coverage": {
                **coverage.__dict__,
                "acceptance_ratio": coverage.acceptance_ratio,
                "code_explainability_ratio": coverage.code_explainability_ratio,
                "passed": coverage.passed,
            },
            "quality_gate": quality_gate.to_dict(),
            "validation_errors": validation_errors,
            "trace_links": [
                {
                    "link_id": link.link_id,
                    "from_ref": link.from_ref,
                    "relation": link.relation.value,
                    "to_ref": link.to_ref,
                    "from_hash": link.from_hash,
                    "to_hash": link.to_hash,
                    "status": link.status,
                }
                for link in links
            ],
            "event_count": len(events),
        }
        path = self.vaf_root / "traces" / f"{state.change_id}-{run_id}.yaml"
        path.write_text(yaml.safe_dump(report, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return report

    def _load_or_create_change(
        self,
        change_id: str,
        title: str,
        objective: str,
        source: str,
        implementation_spec: str | Path | None = None,
    ) -> dict[str, Any]:
        path = self.vaf_root / "changes" / f"{change_id}.yaml"
        if path.exists():
            return self._load_change(change_id)
        if not title.strip() or not objective.strip():
            raise WorkflowError("new Change requires --title and --objective")
        change = {
            "change_id": change_id,
            "title": title,
            "objective": objective,
            "source": source,
            "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if implementation_spec is not None:
            spec_path = Path(implementation_spec).resolve()
            if not spec_path.exists():
                raise WorkflowError(f"implementation spec not found: {spec_path}")
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
            if not isinstance(spec, dict) or not isinstance(spec.get("implementation"), dict):
                raise WorkflowError("implementation spec must contain an implementation mapping")
            change["implementation"] = spec["implementation"]
        path.write_text(yaml.safe_dump(change, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return change

    def _load_change(self, change_id: str) -> dict[str, Any]:
        path = self.vaf_root / "changes" / f"{change_id}.yaml"
        if not path.exists():
            raise WorkflowError(f"change not found: {change_id}")
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise WorkflowError(f"invalid change file: {path}")
        return value

    def _load_verification_command(self) -> tuple[str, tuple[str, ...], str, str]:
        path = self.vaf_root / "manifest.yaml"
        try:
            manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise WorkflowError(f"VAF-MANIFEST-001: cannot read verification manifest: {exc}") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("verification"), dict):
            raise WorkflowError("VAF-MANIFEST-001: manifest requires a verification mapping")
        verification = manifest["verification"]
        commands = verification.get("commands")
        if not isinstance(commands, list) or not commands:
            raise WorkflowError("VAF-MANIFEST-001: verification.commands must be a non-empty list")
        default_command = verification.get("default_command")
        if default_command is None and isinstance(commands[0], dict):
            default_command = commands[0].get("id")
        if not isinstance(default_command, str) or not default_command.strip():
            raise WorkflowError("VAF-MANIFEST-001: verification.default_command is required")
        selected: dict[str, Any] | None = None
        for command in commands:
            if not isinstance(command, dict) or not isinstance(command.get("id"), str):
                raise WorkflowError("VAF-MANIFEST-001: each verification command requires an id")
            if command["id"] == default_command:
                selected = command
        if selected is None:
            raise WorkflowError(f"VAF-MANIFEST-001: verification command not found: {default_command}")
        raw_argv = selected.get("argv")
        if not isinstance(raw_argv, list) or not raw_argv or not all(isinstance(item, str) and item for item in raw_argv):
            raise WorkflowError(f"VAF-MANIFEST-001: invalid argv for verification command: {default_command}")
        network_mode = selected.get("network", "disabled")
        if not isinstance(network_mode, str) or not network_mode:
            raise WorkflowError(f"VAF-MANIFEST-001: invalid network mode for verification command: {default_command}")
        normalized = json.dumps(
            {"command_id": default_command, "argv": raw_argv, "network": network_mode},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        command_spec_hash = f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"
        return default_command, tuple(raw_argv), network_mode, command_spec_hash

    def _write_artifact(self, change_id: str, draft: DraftResult, version: int) -> dict[str, Any]:
        content = draft.content
        ArtifactVersion.from_markdown(content)
        return self._write_versioned_content(change_id, draft.artifact_type, content, version)

    def _write_versioned_content(self, change_id: str, artifact_type: str, content: str, version: int) -> dict[str, Any]:
        path = self.vaf_root / "artifacts" / change_id / f"{artifact_type}-v{version}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        artifact = ArtifactVersion.from_markdown(content)
        return {
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type.value,
            "version": artifact.version,
            "content_hash": artifact.content_hash,
            "path": str(path),
        }

    def _store(self, run_id: str) -> JsonlEventStore:
        path = self.vaf_root / "runs" / run_id / "events.jsonl"
        if not path.exists():
            raise WorkflowError(f"run not found: {run_id}")
        return JsonlEventStore(path)

    @staticmethod
    def _review_source(state: RunState, target_hash: str) -> Path:
        if not target_hash:
            raise WorkflowError("VAF-APPROVAL-001: target_hash is required")
        if target_hash != state.artifact_hash:
            raise WorkflowError("VAF-APPROVAL-STALE: target hash is not the current artifact hash")
        if not state.artifact_path:
            raise WorkflowError("run has no reviewable artifact")
        source = Path(state.artifact_path)
        try:
            actual_hash = ArtifactVersion.from_markdown(source.read_text(encoding="utf-8")).content_hash
        except (OSError, ValueError) as exc:
            raise WorkflowError(f"VAF-APPROVAL-STALE: review artifact is unreadable: {exc}") from exc
        if actual_hash != target_hash:
            raise WorkflowError("VAF-APPROVAL-STALE: artifact changed after review; review it again")
        return source

    def _append_event(self, run_id: str, event: EventEnvelope) -> EventEnvelope:
        appended = self._store(run_id).append(event)
        self._write_run_index(run_id, event.change_id)
        return appended

    def _write_run_index(self, run_id: str, change_id: str) -> None:
        state = self.state(run_id)
        path = self.vaf_root / "runs" / run_id / "index.yaml"
        path.write_text(
            yaml.safe_dump(_json_safe({**state.__dict__, "change_id": change_id}), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _require_git_repo(self) -> None:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            raise WorkflowError(f"not a Git repository: {self.project_root}")

    @staticmethod
    def _require_status(state: RunState, expected: str) -> None:
        if state.status != expected:
            raise WorkflowError(f"expected run status {expected}, got {state.status}")

    @staticmethod
    def _require_transition(state: RunState, command: StageCommand) -> None:
        try:
            transition(StageStatus(state.status), command)
        except (TransitionError, ValueError) as exc:
            raise WorkflowError(f"VAF-STATE-001: {exc}") from exc

def _render_artifact(metadata: dict[str, Any], body: str) -> str:
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip() + "\n---\n\n" + body.lstrip()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _truncate(value: str, limit: int = 8192) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated by VAF]"


def _hash_workspace_file(worktree_path: str | None, relative_path: str) -> str | None:
    if not worktree_path:
        return None
    path = Path(worktree_path) / relative_path
    try:
        content = path.read_bytes()
    except OSError:
        return None
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
