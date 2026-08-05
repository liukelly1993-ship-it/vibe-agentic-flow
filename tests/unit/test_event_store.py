import tempfile
import unittest
from pathlib import Path

from vaf.adapters.jsonl_event_store import JsonlEventStore
from vaf.domain.events import EventEnvelope


class EventStoreTests(unittest.TestCase):
    def test_append_and_verify_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlEventStore(Path(directory) / "events.jsonl")
            first = store.append(
                EventEnvelope.create(
                    event_type="RunStarted",
                    run_id="RUN-001",
                    change_id="CHG-001",
                    payload={"stage": "intake"},
                )
            )
            second = store.append(
                EventEnvelope.create(
                    event_type="StageStarted",
                    run_id="RUN-001",
                    change_id="CHG-001",
                    payload={"stage": "prd"},
                )
            )
            self.assertIsNone(first.previous_event_hash)
            self.assertEqual(second.previous_event_hash, first.event_hash)
            store.verify_chain()
            self.assertEqual(len(store.read_all()), 2)

    def test_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            store = JsonlEventStore(path)
            store.append(
                EventEnvelope.create(
                    event_type="RunStarted",
                    run_id="RUN-001",
                    change_id="CHG-001",
                    payload={"ok": True},
                )
            )
            path.write_text(path.read_text().replace('"ok": true', '"ok": false'), encoding="utf-8")
            with self.assertRaises(ValueError):
                store.verify_chain()
