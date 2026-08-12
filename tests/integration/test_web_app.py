import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from vaf.web.app import create_app


def deliverable_status_prd(*, prototype: bool = True) -> str:
    visual = "![状态页面原型](prototype.png)" if prototype else ""
    return f"""# 服务状态页面 PRD

## 背景、目标与业务价值
为本地服务提供状态页面，目标是让开发人员快速确认后端在线并看到当前交付目标。

## 用户角色和使用场景
开发人员启动前后端后打开首页，查看服务状态和需求摘要。

## 范围、非目标和约束
范围是健康接口、需求摘要接口和状态页面；非目标是账号、业务 CRUD 和云端部署。

## 功能需求
- REQ-001：系统应提供可本地运行的服务状态页面和后端状态接口。

## 验收条件
- AC-001：调用 /api/health 返回 status=ok 和服务名称。
- AC-002：页面显示项目标题、交付目标和后端在线状态。

## 数据和 API
GET /api/health 返回状态；GET /api/requirements 返回标题和目标，不保存业务数据。

## 非功能、安全与隐私
接口不读取密钥和个人信息；本地请求 P95 低于 500ms，错误状态可观察。

## 风险、假设和依赖
假设本机安装 Python 和 Node.js；端口冲突是已知风险。

## 页面原型
{visual}
"""


class WebAppIntegrationTests(unittest.TestCase):
    def test_markdown_upload_runs_to_downloadable_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(directory, compose_runtime=False))
            response = client.post(
                "/api/jobs",
                files={"file": ("prd.md", deliverable_status_prd().encode("utf-8"))},
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
            self.assertTrue(job["result"]["compose_validation"]["passed"])
            self.assertTrue(job["result"]["compose_runtime_validation"]["skipped"])
            files = client.get(f"/api/jobs/{job_id}/files").json()["items"]
            self.assertTrue(any(item["path"] == "frontend/src/App.vue" for item in files))
            self.assertFalse(any(item["path"] == "frontend/src/App.jsx" for item in files))
            self.assertTrue(any(item["path"] == "compose.yaml" for item in files))
            self.assertTrue(any(item["path"] == "backend/Dockerfile" for item in files))
            self.assertTrue(any(item["path"] == "frontend/Dockerfile" for item in files))
            self.assertFalse(any("node_modules" in item["path"] for item in files))
            self.assertFalse(any(item["path"].startswith("frontend/dist/") for item in files))
            self.assertEqual(client.get(f"/api/jobs/{job_id}/download").status_code, 200)
            self.assertEqual(client.get("/").status_code, 200)

    def test_markdown_without_prototype_is_blocked_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(directory, compose_runtime=False))
            response = client.post(
                "/api/jobs",
                files={"file": ("prd.md", deliverable_status_prd(prototype=False).encode("utf-8"))},
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
            self.assertIn("原型图片", job["error"])
            self.assertIsNone(job["generated_path"])
            self.assertEqual(job["quality_gate"]["score"], 85.0)
            self.assertEqual(job["quality_gate"]["decision"], "BLOCKED")

    def test_prd_referencing_existing_system_requests_knowledge_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(directory, compose_runtime=False))
            source = deliverable_status_prd() + "\n接口错误码按照现有 CRM 内部接口文档执行。\n"
            response = client.post(
                "/api/jobs",
                files={"file": ("prd.md", source.encode("utf-8"))},
                data={"title": "Knowledge Required"},
            )
            self.assertEqual(response.status_code, 200)
            job_id = response.json()["job_id"]
            job = response.json()
            for _ in range(120):
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in {"COMPLETED", "FAILED", "BLOCKED"}:
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "BLOCKED")
            self.assertTrue(job["prd_review"]["knowledge_request"]["required"])
            self.assertIn("本地知识", job["error"])
            self.assertIsNone(job["generated_path"])

    def test_allowed_knowledge_base_is_snapshotted_and_propagated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            knowledge = Path(directory) / "knowledge"
            knowledge.mkdir()
            (knowledge / "crm-api.md").write_text(
                "CRM 内部接口文档：健康接口错误码为 CRM-001，服务状态必须可查询。",
                encoding="utf-8",
            )
            source = deliverable_status_prd() + "\n接口错误码按照现有 CRM 内部接口文档执行。\n"
            with patch.dict("os.environ", {"VAF_KNOWLEDGE_ROOTS": directory}):
                client = TestClient(create_app(Path(directory) / "web", compose_runtime=False))
                response = client.post(
                    "/api/jobs",
                    files={"file": ("prd.md", source.encode("utf-8"))},
                    data={"title": "Knowledge Grounded", "knowledge_path": str(knowledge)},
                )
                self.assertEqual(response.status_code, 200)
                job = response.json()
                for _ in range(900):
                    job = client.get(f"/api/jobs/{job['job_id']}").json()
                    if job["status"] in {"COMPLETED", "FAILED", "BLOCKED"}:
                        break
                    time.sleep(0.1)
            self.assertEqual(job["status"], "COMPLETED", job.get("error"))
            snapshot_path = Path(job["result"]["knowledge_snapshot_path"])
            self.assertTrue(snapshot_path.is_file())
            self.assertTrue(job["prd_review"]["knowledge_snapshot"]["snapshot_hash"].startswith("sha256:"))
            prd_artifact = next((Path(job["project_path"]) / ".vaf" / "artifacts").rglob("prd-v1.md"))
            content = prd_artifact.read_text(encoding="utf-8")
            self.assertIn("KB-001", content)
            self.assertIn("crm-api.md", content)
