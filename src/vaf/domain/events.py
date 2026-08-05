"""Append-only event envelope with hash-chain support."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from .ids import EventId, new_id


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class EventEnvelope:
    event_id: EventId
    event_type: str
    schema_version: str
    run_id: str
    change_id: str
    stage_run_id: str | None
    correlation_id: str
    causation_id: str | None
    attempt: int
    actor: str
    occurred_at: datetime
    payload: dict[str, Any]
    payload_hash: str
    previous_event_hash: str | None
    event_hash: str

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        run_id: str,
        change_id: str,
        payload: dict[str, Any],
        stage_run_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        attempt: int = 1,
        actor: str = "system",
        previous_event_hash: str | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> "EventEnvelope":
        occurred_at = occurred_at or datetime.now(timezone.utc)
        payload_hash = _sha256(_canonical_json(payload))
        body = {
            "event_id": event_id or new_id("EVT"),
            "event_type": event_type,
            "schema_version": "1.0",
            "run_id": run_id,
            "change_id": change_id,
            "stage_run_id": stage_run_id,
            "correlation_id": correlation_id or run_id,
            "causation_id": causation_id,
            "attempt": attempt,
            "actor": actor,
            "occurred_at": occurred_at.isoformat(),
            "payload": payload,
            "payload_hash": payload_hash,
            "previous_event_hash": previous_event_hash,
        }
        return cls(
            event_id=EventId(body["event_id"]),
            event_type=event_type,
            schema_version="1.0",
            run_id=run_id,
            change_id=change_id,
            stage_run_id=stage_run_id,
            correlation_id=body["correlation_id"],
            causation_id=causation_id,
            attempt=attempt,
            actor=actor,
            occurred_at=occurred_at,
            payload=payload,
            payload_hash=payload_hash,
            previous_event_hash=previous_event_hash,
            event_hash=_sha256(_canonical_json(body)),
        )

    def with_previous_hash(self, previous_event_hash: str | None) -> "EventEnvelope":
        return EventEnvelope.create(
            event_type=self.event_type,
            run_id=self.run_id,
            change_id=self.change_id,
            payload=self.payload,
            stage_run_id=self.stage_run_id,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            attempt=self.attempt,
            actor=self.actor,
            previous_event_hash=previous_event_hash,
            occurred_at=self.occurred_at,
            event_id=self.event_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "change_id": self.change_id,
            "stage_run_id": self.stage_run_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "attempt": self.attempt,
            "actor": self.actor,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": self.payload,
            "payload_hash": self.payload_hash,
            "previous_event_hash": self.previous_event_hash,
            "event_hash": self.event_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EventEnvelope":
        event = cls.create(
            event_type=data["event_type"],
            run_id=data["run_id"],
            change_id=data["change_id"],
            payload=data["payload"],
            stage_run_id=data.get("stage_run_id"),
            correlation_id=data["correlation_id"],
            causation_id=data.get("causation_id"),
            attempt=data["attempt"],
            actor=data["actor"],
            previous_event_hash=data.get("previous_event_hash"),
            occurred_at=datetime.fromisoformat(data["occurred_at"]),
            event_id=data["event_id"],
        )
        if event.payload_hash != data["payload_hash"] or event.event_hash != data["event_hash"]:
            raise ValueError(f"event hash verification failed: {data['event_id']}")
        return event
