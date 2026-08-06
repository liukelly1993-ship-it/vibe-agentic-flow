import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from vaf.web.app import create_app


class WebAppIntegrationTests(unittest.TestCase):
    def test_markdown_upload_runs_to_downloadable_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(directory))
            response = client.post(
                "/api/jobs",
                files={"file": ("prd.md", b"# Task Board\n\nBuild a local task board with a FastAPI backend and React frontend.")},
                data={"title": "Task Board"},
            )
            self.assertEqual(response.status_code, 200)
            job_id = response.json()["job_id"]
            job = response.json()
            for _ in range(240):
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in {"COMPLETED", "FAILED"}:
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "COMPLETED", job.get("error"))
            self.assertEqual(job["trace_status"], "passed")
            self.assertGreater(job["quality_gate"]["score"], 90)
            self.assertTrue(job["result"]["frontend_validation"]["passed"])
            files = client.get(f"/api/jobs/{job_id}/files").json()["items"]
            self.assertTrue(any(item["path"] == "frontend/src/App.jsx" for item in files))
            self.assertFalse(any("node_modules" in item["path"] for item in files))
            self.assertFalse(any(item["path"].startswith("frontend/dist/") for item in files))
            self.assertEqual(client.get(f"/api/jobs/{job_id}/download").status_code, 200)
            self.assertEqual(client.get("/").status_code, 200)
