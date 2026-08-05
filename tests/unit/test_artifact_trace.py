import unittest

from vaf.domain.artifacts import ArtifactStatus, ArtifactValidationError, ArtifactVersion
from vaf.domain.trace import (
    TraceLink,
    TraceRelation,
    calculate_coverage,
    validate_links,
)


ARTIFACT = """---
artifact_id: PRD-001
artifact_type: prd
change_id: CHG-001
version: 1
status: approved
depends_on: []
requirements: [REQ-001]
created_by: fake-agent
created_at: 2026-08-05T12:00:00Z
approved_by: user
approved_at: 2026-08-05T12:01:00Z
---

# PRD

## REQ-001

The system shall validate the input.
"""


class ArtifactTraceTests(unittest.TestCase):
    def test_approved_artifact_is_versioned_and_hashed(self) -> None:
        artifact = ArtifactVersion.from_markdown(ARTIFACT)
        self.assertEqual(artifact.status, ArtifactStatus.APPROVED)
        self.assertTrue(artifact.content_hash.startswith("sha256:"))

    def test_approved_artifact_requires_approver(self) -> None:
        content = ARTIFACT.replace("approved_by: user\n", "")
        with self.assertRaises(ArtifactValidationError):
            ArtifactVersion.from_markdown(content)

    def test_trace_coverage_requires_passed_test(self) -> None:
        links = [
            TraceLink("L1", "AC-001", TraceRelation.VERIFIES, "TC-001", "a", "t"),
            TraceLink("L2", "FILE:app.py", TraceRelation.IMPLEMENTS, "TASK-001", "f", "task"),
            TraceLink("L3", "TASK-001", TraceRelation.SATISFIES, "REQ-001", "task", "req"),
        ]
        report = calculate_coverage({"AC-001"}, {"TC-001"}, links, {"FILE:app.py"})
        self.assertEqual(report.acceptance_ratio, 1.0)
        self.assertEqual(report.code_explainability_ratio, 1.0)
        self.assertTrue(report.passed)

    def test_stale_link_is_reported(self) -> None:
        link = TraceLink("L1", "REQ-001", TraceRelation.SATISFIES, "AC-001", "old", "ac")
        errors = validate_links([link], {"REQ-001": "new", "AC-001": "ac"})
        self.assertEqual(errors, ["stale trace hash: REQ-001"])
