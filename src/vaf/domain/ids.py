"""Stable identifier helpers."""

from __future__ import annotations

from typing import NewType
from uuid import uuid4

ChangeId = NewType("ChangeId", str)
RunId = NewType("RunId", str)
StageRunId = NewType("StageRunId", str)
ArtifactId = NewType("ArtifactId", str)
TraceLinkId = NewType("TraceLinkId", str)
EventId = NewType("EventId", str)
InvocationId = NewType("InvocationId", str)
PolicyDecisionId = NewType("PolicyDecisionId", str)


def new_id(prefix: str) -> str:
    """Create a human-readable opaque identifier."""

    if not prefix or not prefix.replace("-", "").isalnum():
        raise ValueError("prefix must contain only letters, numbers, and hyphens")
    return f"{prefix}-{uuid4().hex[:12]}"
