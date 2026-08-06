"""Rebuild a run view from its append-only event stream."""

from __future__ import annotations

from dataclasses import dataclass, replace
from vaf.domain.events import EventEnvelope


@dataclass(frozen=True)
class RunState:
    run_id: str
    change_id: str
    status: str
    current_stage: str | None = None
    artifact_id: str | None = None
    artifact_path: str | None = None
    artifact_hash: str | None = None
    artifact_version: int | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None
    changed_paths: tuple[str, ...] = ()
    event_count: int = 0


def project_events(events: list[EventEnvelope]) -> RunState:
    """Project the latest user-visible state without storing mutable run state."""

    if not events:
        raise ValueError("cannot project an empty event stream")
    first = events[0]
    state = RunState(
        run_id=first.run_id,
        change_id=first.change_id,
        status="UNKNOWN",
        event_count=len(events),
    )
    for event in events:
        if event.run_id != state.run_id or event.change_id != state.change_id:
            raise ValueError("event stream contains more than one run or change")
        payload = event.payload
        if event.event_type == "RunStarted":
            state = replace(state, status="RUNNING")
        elif event.event_type == "StageStarted":
            state = replace(state, status="RUNNING", current_stage=str(payload["stage"]))
        elif event.event_type == "ArtifactDrafted":
            state = replace(
                state,
                status="WAITING_REVIEW",
                artifact_id=str(payload["artifact_id"]),
                artifact_path=str(payload["path"]),
                artifact_hash=str(payload["content_hash"]),
                artifact_version=int(payload["version"]),
            )
        elif event.event_type == "ArtifactApproved":
            state = replace(
                state,
                status="APPROVED",
                artifact_id=str(payload["artifact_id"]),
                artifact_path=str(payload["path"]),
                artifact_hash=str(payload["content_hash"]),
                artifact_version=int(payload["version"]),
            )
        elif event.event_type == "ArtifactChangesRequested":
            state = replace(state, status="CHANGES_REQUESTED")
        elif event.event_type == "GateBlocked":
            state = replace(state, status="BLOCKED")
        elif event.event_type == "WorktreeCreated":
            state = replace(
                state,
                status="IMPLEMENTING",
                current_stage="implementation",
                worktree_path=str(payload["path"]),
                worktree_branch=str(payload["branch"]),
            )
        elif event.event_type == "CodeFileWritten":
            state = replace(
                state,
                status="IMPLEMENTING",
                changed_paths=(*state.changed_paths, str(payload["path"])),
            )
        elif event.event_type == "ImplementationCompleted":
            state = replace(
                state,
                status="IMPLEMENTED",
                changed_paths=tuple(str(path) for path in payload.get("changed_paths", state.changed_paths)),
            )
        elif event.event_type == "ImplementationFailed":
            state = replace(state, status="FAILED")
        elif event.event_type == "VerificationCompleted":
            state = replace(state, status="VERIFIED" if payload["exit_code"] == 0 else "FAILED")
        elif event.event_type == "RunCompleted":
            state = replace(state, status=str(payload.get("status", "COMPLETED")))
    return replace(state, event_count=len(events))
