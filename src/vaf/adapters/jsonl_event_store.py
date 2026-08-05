"""Append-only local event store for v0.1."""

from __future__ import annotations

from pathlib import Path
import json
import os

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from vaf.domain.events import EventEnvelope


class JsonlEventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: EventEnvelope) -> EventEnvelope:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                events = self._read_handle(handle)
                previous_hash = events[-1].event_hash if events else None
                if event.previous_event_hash not in (None, previous_hash):
                    raise ValueError("event previous hash does not match the current log tail")
                if event.previous_event_hash is None:
                    event = event.with_previous_hash(previous_hash)
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return event

    def read_all(self) -> list[EventEnvelope]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            return self._read_handle(handle)

    @staticmethod
    def _read_handle(handle) -> list[EventEnvelope]:
        events: list[EventEnvelope] = []
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                events.append(EventEnvelope.from_dict(json.loads(line)))
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid event at line {line_number}") from exc
        return events

    def verify_chain(self) -> None:
        events = self.read_all()
        previous_hash = None
        for event in events:
            if event.previous_event_hash != previous_hash:
                raise ValueError(f"event chain broken at {event.event_id}")
            previous_hash = event.event_hash
