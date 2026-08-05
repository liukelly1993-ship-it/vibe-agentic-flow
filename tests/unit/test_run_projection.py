import unittest

from vaf.application.run_projection import project_events
from vaf.domain.events import EventEnvelope


class RunProjectionTests(unittest.TestCase):
    def test_projects_latest_state_from_event_stream(self) -> None:
        events = [
            EventEnvelope.create(
                event_type="RunStarted",
                run_id="RUN-001",
                change_id="CHG-001",
                payload={"stage": "prd"},
            ),
            EventEnvelope.create(
                event_type="StageStarted",
                run_id="RUN-001",
                change_id="CHG-001",
                payload={"stage": "prd", "attempt": 1},
            ),
            EventEnvelope.create(
                event_type="ArtifactDrafted",
                run_id="RUN-001",
                change_id="CHG-001",
                payload={
                    "artifact_id": "PRD-001",
                    "path": "/tmp/prd.md",
                    "content_hash": "sha256:test",
                    "version": 1,
                },
            ),
        ]
        state = project_events(events)
        self.assertEqual(state.status, "WAITING_REVIEW")
        self.assertEqual(state.current_stage, "prd")
        self.assertEqual(state.artifact_id, "PRD-001")
        self.assertEqual(state.event_count, 3)

    def test_rejects_mixed_run_stream(self) -> None:
        events = [
            EventEnvelope.create(
                event_type="RunStarted",
                run_id="RUN-001",
                change_id="CHG-001",
                payload={},
            ),
            EventEnvelope.create(
                event_type="StageStarted",
                run_id="RUN-002",
                change_id="CHG-001",
                payload={"stage": "prd"},
            ),
        ]
        with self.assertRaises(ValueError):
            project_events(events)
