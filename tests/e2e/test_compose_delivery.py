import os
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from tests.integration.test_web_app import deliverable_status_prd
from vaf.web.app import create_app


@unittest.skipUnless(
    os.environ.get("VAF_RUN_DOCKER_E2E") == "1",
    "set VAF_RUN_DOCKER_E2E=1 to run the real Docker Compose acceptance",
)
class ComposeDeliveryE2ETests(unittest.TestCase):
    def test_generated_project_builds_runs_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(directory, compose_runtime=True))
            response = client.post(
                "/api/jobs",
                files={"file": ("prd.md", deliverable_status_prd().encode("utf-8"))},
                data={"title": "Compose Runtime Acceptance"},
            )
            self.assertEqual(response.status_code, 200)
            job = response.json()
            for _ in range(1200):
                job = client.get(f"/api/jobs/{job['job_id']}").json()
                if job["status"] in {"COMPLETED", "FAILED", "BLOCKED"}:
                    break
                time.sleep(0.5)
            self.assertEqual(job["status"], "COMPLETED", job.get("error"))
            runtime = job["result"]["compose_runtime_validation"]
            self.assertTrue(runtime["passed"])
            self.assertFalse(runtime["skipped"])
            self.assertEqual(runtime["cleanup"], "passed")
            self.assertEqual(
                runtime["checks"],
                ["containers-healthy", "backend-api", "frontend-http"],
            )


if __name__ == "__main__":
    unittest.main()
