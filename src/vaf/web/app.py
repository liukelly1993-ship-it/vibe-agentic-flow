"""FastAPI control plane for the local, score-gated VAF workflow."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
import zipfile

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from vaf.application.local_workflow import LocalWorkflow, WorkflowError
from vaf.domain.ids import new_id
from vaf.web.ingestion import DocumentIngestionError, IngestedDocument, ingest_feishu, ingest_upload
from vaf.web.prd_agent import PrdContext, PrdTemplateAgent
from vaf.web.stacks import choose_stack
from vaf.web.store import JobStore


class VafWebService:
    def __init__(self, data_root: str | Path | None = None) -> None:
        self.data_root = Path(data_root or os.environ.get("VAF_WEB_ROOT", ".vaf-web")).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(self.data_root / "jobs.sqlite3")
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vaf-job")

    def create_job(self, document: IngestedDocument, title: str | None) -> dict[str, Any]:
        job_id = new_id("JOB")
        job_root = self.data_root / "jobs" / job_id
        job_root.mkdir(parents=True, exist_ok=False)
        source_path = job_root / f"source{Path(document.name).suffix.lower() or '.md'}"
        source_path.write_text(document.text, encoding="utf-8")
        choice = choose_stack(document.text)
        record = self.store.create(
            {
                "job_id": job_id,
                "title": title.strip() if title and title.strip() else _infer_title(document),
                "source_type": document.source_type,
                "source_ref": document.source_ref,
                "source_hash": document.content_hash,
                "project_path": str(job_root / "project"),
                "stack": choice.to_dict(),
            }
        )
        self.executor.submit(self._run_job, job_id, document, record["title"], choice)
        return _public_job(record)

    def _run_job(self, job_id: str, document: IngestedDocument, title: str, choice: Any) -> None:
        job_root = self.data_root / "jobs" / job_id
        project_path = job_root / "project"
        implementation_path = job_root / "implementation.yaml"
        try:
            self.store.update(job_id, status="RUNNING", phase="preparing-project")
            _initialize_git_repo(project_path)
            context = PrdContext(
                title=title,
                objective=_infer_objective(document.text),
                source_text=document.text,
                source_hash=document.content_hash,
                stack=choice,
            )
            agent = PrdTemplateAgent(context)
            implementation_path.write_text(
                _implementation_yaml(agent.implementation_items()), encoding="utf-8"
            )
            self.store.update(job_id, phase="score-gated-generation")
            workflow = LocalWorkflow(project_path, agent=agent)
            result = workflow.autopilot(
                change_id=f"CHG-{job_id.split('-')[-1]}",
                title=title,
                objective=context.objective,
                source=f"{document.source_type}:{document.source_ref}",
                implementation_spec=implementation_path,
                progress_callback=lambda progress: self._update_progress(job_id, progress),
            )
            state = result.get("state", {})
            generated_path = state.get("worktree_path")
            frontend_validation = _verify_frontend_build(generated_path)
            if not frontend_validation["passed"]:
                raise WorkflowError(
                    f"VAF-FRONTEND-001: {frontend_validation.get('error', '前端构建失败')}"
                )
            self.store.update(
                job_id,
                status="COMPLETED",
                phase="completed",
                generated_path=generated_path,
                result={
                    "run_id": state.get("run_id"),
                    "state": state,
                    "trace": result.get("trace", {}),
                    "frontend_validation": frontend_validation,
                    "source_path": str(job_root / f"source{Path(document.name).suffix.lower() or '.md'}"),
                    "implementation_path": str(implementation_path),
                },
            )
        except Exception as exc:
            self.store.update(job_id, status="FAILED", phase="failed", error=_safe_error(exc))

    def _update_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        self.store.update(
            job_id,
            phase="score-gated-generation",
            result={"progress": progress},
        )

    def get(self, job_id: str) -> dict[str, Any]:
        record = self.store.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _public_job(record)


def create_app(data_root: str | Path | None = None) -> FastAPI:
    service = VafWebService(data_root)
    app = FastAPI(title="VAF Control Plane", version="0.2.0")
    app.state.vaf = service

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "service": "vaf-web", "data_root": str(service.data_root)}

    @app.get("/api/jobs")
    def list_jobs() -> dict[str, object]:
        return {"items": [_public_job(item) for item in service.store.list()]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        return service.get(job_id)

    @app.post("/api/jobs")
    async def create_job(
        file: UploadFile | None = File(default=None),
        feishu_url: str | None = Form(default=None),
        title: str | None = Form(default=None),
    ) -> dict[str, Any]:
        if file is None and not (feishu_url and feishu_url.strip()):
            raise HTTPException(status_code=400, detail="请上传 PRD 文件或填写飞书文档链接")
        try:
            if file is not None:
                document = ingest_upload(file.filename or "prd.md", await file.read())
            else:
                document = ingest_feishu(feishu_url or "")
            return service.create_job(document, title)
        except DocumentIngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/jobs/from-feishu")
    def create_feishu_job(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        url = str(payload.get("url", "")).strip()
        try:
            document = ingest_feishu(url)
            return service.create_job(document, str(payload.get("title", "")))
        except DocumentIngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/files")
    def list_generated_files(job_id: str) -> dict[str, object]:
        record = service.store.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="job not found")
        generated_path = record.get("generated_path")
        if not generated_path:
            return {"items": []}
        root = Path(str(generated_path))
        if not root.is_dir():
            return {"items": []}
        items = []
        for path in sorted(root.rglob("*")):
            if (
                not path.is_file()
                or ".git" in path.parts
                or ".vaf" in path.parts
                or "__pycache__" in path.parts
                or "node_modules" in path.parts
                or "dist" in path.parts
            ):
                continue
            items.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size})
        return {"items": items[:500]}

    @app.get("/api/jobs/{job_id}/download")
    def download_generated_project(job_id: str) -> StreamingResponse:
        record = service.store.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="job not found")
        generated_path = record.get("generated_path")
        if not generated_path:
            raise HTTPException(status_code=409, detail="生成项目尚未完成")
        root = Path(str(generated_path))
        if not root.is_dir():
            raise HTTPException(status_code=409, detail="生成项目尚未完成")
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if (
                    not path.is_file()
                    or ".git" in path.parts
                    or ".vaf" in path.parts
                    or "__pycache__" in path.parts
                    or "node_modules" in path.parts
                    or "dist" in path.parts
                ):
                    continue
                archive.write(path, path.relative_to(root))
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{job_id}-project.zip"'},
        )

    static_root = Path(__file__).parent / "static"

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_root / "index.html")

    app.mount("/static", StaticFiles(directory=static_root), name="static")
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        "vaf.web.app:create_app",
        factory=True,
        host=os.environ.get("VAF_HOST", "127.0.0.1"),
        port=int(os.environ.get("VAF_PORT", "8787")),
    )


def _initialize_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)
    (path / "README.md").write_text("# VAF generated project\n", encoding="utf-8")
    (path / ".gitignore").write_text(".venv/\nnode_modules/\n__pycache__/\n.vaf/\n", encoding="utf-8")
    _run_git(path, "init", "-q")
    _run_git(path, "config", "user.email", "vaf@localhost.invalid")
    _run_git(path, "config", "user.name", "VAF Local Agent")
    _run_git(path, "add", "README.md", ".gitignore")
    _run_git(path, "commit", "-qm", "chore: initialize generated project")


def _run_git(cwd: Path, *args: str) -> None:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False, shell=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git initialization failed")


def _implementation_yaml(items: list[dict[str, object]]) -> str:
    import yaml

    return yaml.safe_dump({"implementation": {"changes": items}}, allow_unicode=True, sort_keys=False)


def _infer_title(document: IngestedDocument) -> str:
    in_frontmatter = False
    for line in document.text.splitlines():
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        value = re.sub(r"^#+\s*", "", stripped)
        if value and not value.startswith("---"):
            return value[:120]
    return Path(document.name).stem or "VAF Generated Project"


def _infer_objective(text: str) -> str:
    in_frontmatter = False
    values: list[str] = []
    for line in text.splitlines():
        if line.strip() == "---":
            in_frontmatter = not in_frontmatter
            continue
        if not in_frontmatter and line.strip():
            values.append(line.strip(" -*#\t"))
    values = [value for value in values if value not in {"---", "```"}]
    return " ".join(values[:3])[:800] or "根据上传的 PRD 生成可本地运行的前后端应用。"


def _public_job(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result", {})
    trace = result.get("trace", {}) if isinstance(result, dict) else {}
    return {
        "job_id": record.get("job_id"),
        "title": record.get("title"),
        "source_type": record.get("source_type"),
        "source_hash": record.get("source_hash"),
        "status": record.get("status"),
        "phase": record.get("phase"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "project_path": record.get("project_path"),
        "generated_path": record.get("generated_path"),
        "stack": record.get("stack", {}),
        "error": record.get("error"),
        "run_id": result.get("run_id") if isinstance(result, dict) else None,
        "trace_status": trace.get("status"),
        "quality_gate": trace.get("quality_gate", {}),
        "progress": result.get("progress", {}) if isinstance(result, dict) else {},
        "result": result,
    }


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, WorkflowError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _verify_frontend_build(generated_path: object) -> dict[str, object]:
    if not isinstance(generated_path, str) or not generated_path:
        return {"passed": False, "error": "生成工作区路径缺失"}
    source = Path(generated_path) / "frontend"
    package = source / "package.json"
    if not source.is_dir() or not package.is_file():
        return {"passed": False, "error": "生成项目缺少 frontend/package.json"}
    npm = shutil.which("npm")
    if npm is None:
        return {"passed": False, "error": "未找到 npm，无法验证前端构建"}
    with tempfile.TemporaryDirectory(prefix="vaf-frontend-verify-") as directory:
        isolated = Path(directory) / "frontend"
        npm_environment = os.environ.copy()
        npm_environment["NPM_CONFIG_CACHE"] = str(Path(directory) / "npm-cache")
        shutil.copytree(
            source,
            isolated,
            ignore=shutil.ignore_patterns("node_modules", "dist"),
        )
        install = subprocess.run(
            [npm, "install", "--legacy-peer-deps", "--no-audit", "--no-fund"],
            cwd=isolated,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=npm_environment,
        )
        if install.returncode != 0:
            return {
                "passed": False,
                "command": "npm install --legacy-peer-deps --no-audit --no-fund",
                "exit_code": install.returncode,
                "error": _truncate_output(install.stderr or install.stdout),
            }
        build = subprocess.run(
            [npm, "run", "build"],
            cwd=isolated,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=npm_environment,
        )
        if build.returncode != 0:
            return {
                "passed": False,
                "command": "npm run build",
                "exit_code": build.returncode,
                "error": _truncate_output(build.stderr or build.stdout),
            }
    return {
        "passed": True,
        "commands": [
            "npm install --legacy-peer-deps --no-audit --no-fund",
            "npm run build",
        ],
        "exit_code": 0,
    }


def _truncate_output(value: str, limit: int = 2000) -> str:
    text = value.strip()
    return text if len(text) <= limit else text[-limit:]


if __name__ == "__main__":
    main()
