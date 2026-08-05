import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vaf.adapters.git_worktree import GitWorktreeManager
from vaf.adapters.jsonl_event_store import JsonlEventStore
from vaf.adapters.tool_gateway import LocalFileAdapter, ToolGateway
from vaf.application.local_workflow import LocalWorkflow
from vaf.domain.events import EventEnvelope
from vaf.policy.engine import PolicyEngine, ToolRequest


class LocalWorkflowCliTests(unittest.TestCase):
    def test_approve_rejects_stale_target_hash(self) -> None:
        project = Path(__file__).parents[2]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "demo"
            self._init_repo(repo)
            state = json.loads(
                self._cli(
                    project,
                    repo,
                    "run",
                    "--change",
                    "CHG-STALE-APPROVAL",
                    "--title",
                    "Stale approval",
                    "--objective",
                    "验证审批绑定版本",
                )
            )
            artifact_path = Path(state["artifact_path"])
            artifact_path.write_text(artifact_path.read_text(encoding="utf-8") + "\n人工修改\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "vaf.cli",
                    "--path",
                    str(repo),
                    "approve",
                    "--run",
                    state["run_id"],
                    "--actor",
                    "reviewer",
                    "--target-hash",
                    state["artifact_hash"],
                ],
                cwd=project,
                env={**os.environ, "PYTHONPATH": str(project / "src")},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("VAF-APPROVAL-STALE", result.stderr)

    def test_init_run_reject_resume_and_review(self) -> None:
        project = Path(__file__).parents[2]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "demo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "vaf@example.invalid")
            self._git(repo, "config", "user.name", "VAF Test")
            (repo / "README.md").write_text("demo\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-m", "initial")

            run_output = self._cli(project, repo, "run", "--change", "CHG-001", "--title", "Demo", "--objective", "验证 CLI 闭环")
            state = json.loads(run_output)
            self.assertEqual(state["status"], "WAITING_REVIEW")
            run_id = state["run_id"]

            rejected = json.loads(
                self._cli(
                    project,
                    repo,
                    "reject",
                    "--run",
                    run_id,
                    "--target-hash",
                    state["artifact_hash"],
                    "--comment",
                    "补充异常场景",
                )
            )
            self.assertEqual(rejected["status"], "CHANGES_REQUESTED")
            resumed = json.loads(self._cli(project, repo, "resume", "--run", run_id))
            self.assertEqual(resumed["status"], "WAITING_REVIEW")
            reviewed = json.loads(self._cli(project, repo, "review", "--run", run_id))
            self.assertEqual(reviewed["metadata"]["status"], "waiting_review")

    def test_approve_full_pipeline_verify_and_trace(self) -> None:
        project = Path(__file__).parents[2]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "demo"
            self._init_repo(repo)
            tests = repo / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_demo.py").write_text(
                "import unittest\n\nclass DemoTests(unittest.TestCase):\n    def test_smoke(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            state = json.loads(self._cli(project, repo, "run", "--change", "CHG-002", "--title", "Demo", "--objective", "验证主流程"))
            run_id = state["run_id"]
            self.assertEqual(state["status"], "WAITING_REVIEW")

            state = self._approve(project, repo, run_id)
            self.assertEqual(state["status"], "APPROVED")
            for stage in ("technical-design", "test-cases", "implementation-plan"):
                state = json.loads(self._cli(project, repo, "resume", "--run", run_id))
                self.assertEqual(state["status"], "WAITING_REVIEW", stage)
                state = self._approve(project, repo, run_id)
                self.assertEqual(state["status"], "APPROVED", stage)

            ready = json.loads(self._cli(project, repo, "resume", "--run", run_id))
            self.assertEqual(ready["status"], "READY_FOR_IMPLEMENTATION")
            verify_before_implementation = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "vaf.cli",
                    "--path",
                    str(repo),
                    "verify",
                    "--run",
                    run_id,
                ],
                cwd=project,
                env={**os.environ, "PYTHONPATH": str(project / "src")},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verify_before_implementation.returncode, 2)
            self.assertIn("VAF-STATE-001", verify_before_implementation.stderr)
            trace = json.loads(self._cli(project, repo, "trace", "--run", run_id))
            self.assertEqual(trace["status"], "incomplete")
            self.assertGreaterEqual(trace["event_count"], 14)

    def test_implementation_writes_only_declared_files_in_worktree(self) -> None:
        project = Path(__file__).parents[2]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "demo"
            self._init_repo(repo)
            spec = Path(directory) / "implementation.yaml"
            spec.write_text(
                """implementation:
  changes:
    - task_id: TASK-001
      path: generated.py
      requirement_ids: [REQ-001]
      content: |
        VALUE = 1
    - task_id: TASK-001
      path: tests/test_generated.py
      acceptance_ids: [AC-001, AC-002]
      test_ids: [TC-001]
      content: |
        import unittest
        import generated

        class GeneratedTests(unittest.TestCase):
            def test_value(self):
                self.assertEqual(generated.VALUE, 1)
    - task_id: TASK-001
      path: tests/__init__.py
      content: |
""",
                encoding="utf-8",
            )
            state = json.loads(
                self._cli(
                    project,
                    repo,
                    "run",
                    "--change",
                    "CHG-003",
                    "--title",
                    "Generated change",
                    "--objective",
                    "验证代码变更",
                    "--implementation-spec",
                    str(spec),
                )
            )
            run_id = state["run_id"]
            for stage in ("prd", "technical-design", "test-cases"):
                state = self._approve(project, repo, run_id)
                self.assertEqual(state["status"], "APPROVED", stage)
                state = json.loads(self._cli(project, repo, "resume", "--run", run_id))
            state = self._approve(project, repo, run_id)
            self.assertEqual(state["status"], "APPROVED")
            state = json.loads(self._cli(project, repo, "resume", "--run", run_id))
            self.assertEqual(state["status"], "READY_FOR_IMPLEMENTATION")
            implemented = json.loads(self._cli(project, repo, "implement", "--run", run_id))
            self.assertEqual(implemented["status"], "IMPLEMENTED")
            worktree = Path(implemented["worktree_path"])
            self.assertTrue((worktree / "generated.py").exists())
            self.assertFalse((repo / "generated.py").exists())
            verified = json.loads(self._cli(project, repo, "verify", "--run", run_id))
            self.assertEqual(verified["status"], "VERIFIED")
            trace = json.loads(self._cli(project, repo, "trace", "--run", run_id))
            self.assertEqual(trace["status"], "passed")
            self.assertTrue(trace["verification_matches_workspace"])
            self.assertEqual(trace["coverage"]["acceptance_ratio"], 1.0)
            self.assertEqual(trace["coverage"]["code_explainability_ratio"], 1.0)
            verified_again = json.loads(self._cli(project, repo, "verify", "--run", run_id))
            self.assertEqual(verified_again["status"], "VERIFIED")
            (worktree / "tests" / "test_generated.py").write_text(
                "import unittest\n\nclass GeneratedTests(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1, 2)\n",
                encoding="utf-8",
            )
            verified_after_change = json.loads(self._cli(project, repo, "verify", "--run", run_id))
            self.assertEqual(verified_after_change["status"], "FAILED")
            trace_after_change = json.loads(self._cli(project, repo, "trace", "--run", run_id))
            self.assertEqual(trace_after_change["status"], "incomplete")
            self.assertTrue(trace_after_change["verification_matches_workspace"])

    def test_implementation_recovers_after_first_file_write(self) -> None:
        project = Path(__file__).parents[2]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "demo"
            self._init_repo(repo)
            spec = Path(directory) / "implementation.yaml"
            spec.write_text(
                """implementation:
  changes:
    - task_id: TASK-001
      path: generated.py
      content: |
        VALUE = 1
    - task_id: TASK-001
      path: tests/__init__.py
      content: |
    - task_id: TASK-001
      path: tests/test_generated.py
      content: |
        import unittest
        import generated

        class GeneratedTests(unittest.TestCase):
            def test_value(self):
                self.assertEqual(generated.VALUE, 1)
""",
                encoding="utf-8",
            )
            state = json.loads(
                self._cli(
                    project,
                    repo,
                    "run",
                    "--change",
                    "CHG-004",
                    "--title",
                    "Recover generated change",
                    "--objective",
                    "验证中断恢复",
                    "--implementation-spec",
                    str(spec),
                )
            )
            run_id = state["run_id"]
            for _stage in ("technical-design", "test-cases", "implementation-plan"):
                self._approve(project, repo, run_id)
                self._cli(project, repo, "resume", "--run", run_id)
            self._approve(project, repo, run_id)
            self._cli(project, repo, "resume", "--run", run_id)

            workflow = LocalWorkflow(repo)
            manager = GitWorktreeManager(repo)
            handle = manager.create("CHG-004", run_id)
            store = JsonlEventStore(repo / ".vaf" / "runs" / run_id / "events.jsonl")
            store.append(
                EventEnvelope.create(
                    event_type="StageStarted",
                    run_id=run_id,
                    change_id="CHG-004",
                    stage_run_id="implementation",
                    payload={"stage": "implementation", "attempt": 1},
                )
            )
            store.append(
                EventEnvelope.create(
                    event_type="WorktreeCreated",
                    run_id=run_id,
                    change_id="CHG-004",
                    stage_run_id="implementation",
                    payload={"path": str(handle.path), "branch": handle.branch},
                )
            )
            gateway = ToolGateway(PolicyEngine(), {"write_file": LocalFileAdapter()}, store)
            first_content = "VALUE = 1\n"
            gateway.execute(
                ToolRequest(
                    tool_name="write_file",
                    args={"path": "generated.py", "content": first_content},
                    workspace_root=handle.path,
                    run_id=run_id,
                    change_id="CHG-004",
                    stage_run_id="implementation",
                    idempotency_key=(
                        f"write:{run_id}:generated.py:"
                        f"{hashlib.sha256(first_content.encode('utf-8')).hexdigest()}"
                    ),
                )
            )
            store.append(
                EventEnvelope.create(
                    event_type="CodeFileWritten",
                    run_id=run_id,
                    change_id="CHG-004",
                    stage_run_id="implementation",
                    payload={"task_id": "TASK-001", "path": "generated.py"},
                )
            )

            recovered = workflow.implement(run_id)
            self.assertEqual(recovered.status, "IMPLEMENTED")
            self.assertEqual(set(recovered.changed_paths), {"generated.py", "tests/__init__.py", "tests/test_generated.py"})
            events = [
                json.loads(line)
                for line in (repo / ".vaf" / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                sum(event["event_type"] == "ToolInvocationRequested" for event in events),
                3,
            )

    def _init_repo(self, repo: Path) -> None:
        repo.mkdir()
        self._git(repo, "init")
        self._git(repo, "config", "user.email", "vaf@example.invalid")
        self._git(repo, "config", "user.name", "VAF Test")
        (repo / "README.md").write_text("demo\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "initial")

    @staticmethod
    def _cli(project: Path, repo: Path, *args: str) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "vaf.cli", "--path", str(repo), *args],
            cwd=project,
            env={**os.environ, "PYTHONPATH": str(project / "src")},
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout

    def _approve(self, project: Path, repo: Path, run_id: str) -> dict[str, object]:
        state = json.loads(self._cli(project, repo, "status", "--run", run_id))
        return json.loads(
            self._cli(
                project,
                repo,
                "approve",
                "--run",
                run_id,
                "--actor",
                "reviewer",
                "--target-hash",
                state["artifact_hash"],
            )
        )

    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
