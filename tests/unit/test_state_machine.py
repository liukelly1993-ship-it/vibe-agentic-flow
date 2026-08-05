import unittest

from vaf.domain.states import StageCommand, StageStatus, TransitionError, transition


class StateMachineTests(unittest.TestCase):
    def test_approval_flow(self) -> None:
        status = transition(StageStatus.PENDING, StageCommand.START)
        status = transition(status, StageCommand.DRAFT_PRODUCED)
        self.assertEqual(transition(status, StageCommand.APPROVE), StageStatus.APPROVED)

    def test_changes_requested_can_regenerate(self) -> None:
        status = transition(StageStatus.WAITING_REVIEW, StageCommand.REQUEST_CHANGES)
        self.assertEqual(transition(status, StageCommand.REGENERATE), StageStatus.RUNNING)

    def test_invalid_transition_is_rejected(self) -> None:
        with self.assertRaises(TransitionError):
            transition(StageStatus.PENDING, StageCommand.APPROVE)

    def test_approved_stage_can_be_invalidated(self) -> None:
        self.assertEqual(
            transition(StageStatus.APPROVED, StageCommand.INVALIDATE),
            StageStatus.INVALIDATED,
        )

    def test_approved_stage_can_advance_to_next_stage(self) -> None:
        self.assertEqual(
            transition(StageStatus.APPROVED, StageCommand.ADVANCE),
            StageStatus.RUNNING,
        )
