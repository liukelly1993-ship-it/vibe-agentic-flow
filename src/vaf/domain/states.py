"""Workflow state and deterministic transition rules."""

from __future__ import annotations

from enum import StrEnum


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_REVIEW = "WAITING_REVIEW"
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    CANCELLED = "CANCELLED"
    ABORTED = "ABORTED"
    INVALIDATED = "INVALIDATED"


class StageCommand(StrEnum):
    START = "StartStage"
    NEED_INPUT = "NeedInput"
    DRAFT_PRODUCED = "DraftProduced"
    VERIFICATION_FAILED = "VerificationFailed"
    CANCEL = "CancelRun"
    APPROVE = "Approve"
    REQUEST_CHANGES = "RequestChanges"
    REJECT = "Reject"
    REGENERATE = "Regenerate"
    RETRY = "Retry"
    RETRY_STARTED = "RetryStarted"
    ADVANCE = "AdvanceStage"
    INVALIDATE = "UpstreamInvalidated"
    ABORT = "Abort"


class TransitionError(ValueError):
    """Raised when a stage command violates the state machine."""


_TRANSITIONS: dict[tuple[StageStatus, StageCommand], StageStatus] = {
    (StageStatus.PENDING, StageCommand.START): StageStatus.RUNNING,
    (StageStatus.RUNNING, StageCommand.NEED_INPUT): StageStatus.BLOCKED,
    (StageStatus.RUNNING, StageCommand.DRAFT_PRODUCED): StageStatus.WAITING_REVIEW,
    (StageStatus.RUNNING, StageCommand.VERIFICATION_FAILED): StageStatus.FAILED,
    (StageStatus.RUNNING, StageCommand.CANCEL): StageStatus.CANCELLED,
    (StageStatus.WAITING_REVIEW, StageCommand.APPROVE): StageStatus.APPROVED,
    (StageStatus.WAITING_REVIEW, StageCommand.REQUEST_CHANGES): StageStatus.CHANGES_REQUESTED,
    (StageStatus.WAITING_REVIEW, StageCommand.REJECT): StageStatus.REJECTED,
    (StageStatus.WAITING_REVIEW, StageCommand.CANCEL): StageStatus.CANCELLED,
    (StageStatus.CHANGES_REQUESTED, StageCommand.REGENERATE): StageStatus.RUNNING,
    (StageStatus.FAILED, StageCommand.RETRY): StageStatus.RETRYING,
    (StageStatus.RETRYING, StageCommand.RETRY_STARTED): StageStatus.RUNNING,
    (StageStatus.APPROVED, StageCommand.ADVANCE): StageStatus.RUNNING,
    (StageStatus.APPROVED, StageCommand.INVALIDATE): StageStatus.INVALIDATED,
    (StageStatus.BLOCKED, StageCommand.ABORT): StageStatus.ABORTED,
    (StageStatus.FAILED, StageCommand.ABORT): StageStatus.ABORTED,
}


def transition(current: StageStatus, command: StageCommand) -> StageStatus:
    """Return the next state without mutating any external state."""

    try:
        return _TRANSITIONS[(current, command)]
    except KeyError as exc:
        raise TransitionError(
            f"illegal stage transition: {current.value} + {command.value}"
        ) from exc
