import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from vaf.web.app import create_app
from tests.unit.test_prd_review import complete_prd


class WebAppIntegrationTests(unittest.TestCase):
    def test_markdown_upload_runs_to_downloadable_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(directory))
            response = client.post(
                "/api/jobs",
                files={"file": ("prd.md", complete_prd().encode("utf-8"))},
                data={"title": "Task Board"},
            )
            self.assertEqual(response.status_code, 200)
            job_id = response.json()["job_id"]
            job = response.json()
            for _ in range(900):
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in {"COMPLETED", "FAILED", "BLOCKED"}:
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "COMPLETED", job.get("error"))
            self.assertEqual(job["trace_status"], "passed")
            self.assertGreater(job["quality_gate"]["score"], 90)
            self.assertTrue(job["result"]["frontend_validation"]["passed"])
            files = client.get(f"/api/jobs/{job_id}/files").json()["items"]
            self.assertTrue(any(item["path"] == "frontend/src/App.vue" for item in files))
            self.assertFalse(any(item["path"] == "frontend/src/App.jsx" for item in files))
            self.assertFalse(any("node_modules" in item["path"] for item in files))
            self.assertFalse(any(item["path"].startswith("frontend/dist/") for item in files))
            self.assertEqual(client.get(f"/api/jobs/{job_id}/download").status_code, 200)
            self.assertEqual(client.get("/").status_code, 200)

    def test_markdown_without_prototype_is_blocked_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(directory))
            response = client.post(
                "/api/jobs",
                files={"file": ("prd.md", complete_prd(prototype=False).encode("utf-8"))},
                data={"title": "Missing Prototype"},
            )
            self.assertEqual(response.status_code, 200)
            job_id = response.json()["job_id"]
            job = response.json()
            for _ in range(120):
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in {"COMPLETED", "FAILED", "BLOCKED"}:
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "BLOCKED", job.get("error"))
            self.assertIn("原型图片或页面截图", job["error"])
            self.assertIsNone(job["generated_path"])
            self.assertEqual(job["quality_gate"]["score"], 85.0)
            self.assertEqual(job["quality_gate"]["decision"], "BLOCKED")
