"""Versioned Markdown artifacts with YAML frontmatter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from typing import Any

import yaml


class ArtifactValidationError(ValueError):
    """Raised when an artifact does not satisfy its envelope contract."""


class ArtifactStatus(StrEnum):
    DRAFT = "draft"
    WAITING_REVIEW = "waiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"


class ArtifactType(StrEnum):
    INTAKE = "intake"
    PRD = "prd"
    TECHNICAL_DESIGN = "technical-design"
    TEST_CASES = "test-cases"
    IMPLEMENTATION_PLAN = "implementation-plan"
    VERIFICATION_REPORT = "verification-report"
    TRACE_REPORT = "trace-report"


@dataclass(frozen=True)
class ArtifactVersion:
    artifact_id: str
    artifact_type: ArtifactType
    change_id: str
    version: int
    status: ArtifactStatus
    content_hash: str
    depends_on: tuple[tuple[str, str], ...]
    requirements: tuple[str, ...]
    created_by: str
    created_at: str
    approved_by: str | None
    approved_at: str | None

    @classmethod
    def from_markdown(cls, content: str) -> "ArtifactVersion":
        metadata, _body = split_frontmatter(content)
        required = {
            "artifact_id",
            "artifact_type",
            "change_id",
            "version",
            "status",
            "created_by",
            "created_at",
        }
        missing = sorted(required - metadata.keys())
        if missing:
            raise ArtifactValidationError(f"missing frontmatter fields: {', '.join(missing)}")

        try:
            artifact_type = ArtifactType(metadata["artifact_type"])
            status = ArtifactStatus(metadata["status"])
            version = int(metadata["version"])
        except (TypeError, ValueError) as exc:
            raise ArtifactValidationError("invalid artifact_type, status, or version") from exc
        if version < 1:
            raise ArtifactValidationError("version must be >= 1")

        dependencies: list[tuple[str, str]] = []
        for item in metadata.get("depends_on", []):
            if not isinstance(item, dict) or not item.get("artifact_id") or not item.get("content_hash"):
                raise ArtifactValidationError("depends_on entries require artifact_id and content_hash")
            dependencies.append((str(item["artifact_id"]), str(item["content_hash"])))

        requirements = metadata.get("requirements", [])
        if not isinstance(requirements, list) or not all(isinstance(item, str) for item in requirements):
            raise ArtifactValidationError("requirements must be a list of strings")

        if status == ArtifactStatus.APPROVED and not metadata.get("approved_by"):
            raise ArtifactValidationError("approved artifacts require approved_by")

        content_hash = f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"
        return cls(
            artifact_id=str(metadata["artifact_id"]),
            artifact_type=artifact_type,
            change_id=str(metadata["change_id"]),
            version=version,
            status=status,
            content_hash=content_hash,
            depends_on=tuple(dependencies),
            requirements=tuple(requirements),
            created_by=str(metadata["created_by"]),
            created_at=str(metadata["created_at"]),
            approved_by=str(metadata["approved_by"]) if metadata.get("approved_by") else None,
            approved_at=str(metadata["approved_at"]) if metadata.get("approved_at") else None,
        )


def split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        raise ArtifactValidationError("artifact must start with YAML frontmatter")
    marker = "\n---\n"
    end = content.find(marker, 4)
    if end == -1:
        raise ArtifactValidationError("artifact frontmatter is not closed")
    raw = content[4:end]
    metadata = yaml.safe_load(raw)
    if not isinstance(metadata, dict):
        raise ArtifactValidationError("frontmatter must be a YAML mapping")
    body = content[end + len(marker) :]
    if not body.strip():
        raise ArtifactValidationError("artifact body must not be empty")
    return metadata, body
