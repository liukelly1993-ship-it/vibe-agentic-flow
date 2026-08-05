import subprocess
import tempfile
import unittest
from pathlib import Path

from vaf.adapters.git_worktree import GitWorktreeManager, GitWorktreeError


class GitWorktreeTests(unittest.TestCase):
    def test_create_scope_check_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "vaf@example.invalid")
            self._git(repo, "config", "user.name", "VAF Test")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-m", "initial")

            manager = GitWorktreeManager(repo, Path(directory) / "worktrees")
            handle = manager.create("CHG-001", "RUN-001")
            (handle.path / "allowed.py").write_text("value = 1\n", encoding="utf-8")
            manager.assert_allowed_changes(handle, {"allowed.py"})
            with self.assertRaises(GitWorktreeError):
                manager.assert_allowed_changes(handle, {"other.py"})
            manager.release(handle, force=True)
            self.assertFalse(handle.path.exists())

    def test_create_rejects_uncommitted_project_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "vaf@example.invalid")
            self._git(repo, "config", "user.name", "VAF Test")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-m", "initial")
            (repo / "README.md").write_text("uncommitted\n", encoding="utf-8")

            with self.assertRaises(GitWorktreeError):
                GitWorktreeManager(repo, Path(directory) / "worktrees").create("CHG-001", "RUN-001")

    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
