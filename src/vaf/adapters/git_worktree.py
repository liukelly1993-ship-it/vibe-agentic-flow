"""Git worktree adapter with explicit locks and change-scope checks."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import json

from vaf.ports.tools import ToolExecutionOutput


class GitWorktreeError(RuntimeError):
    """Raised when a worktree operation cannot be completed safely."""


@dataclass(frozen=True)
class RepoSnapshot:
    root: Path
    head: str
    branch: str | None
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class WorktreeHandle:
    change_id: str
    run_id: str
    path: Path
    branch: str
    lock_path: Path


class GitWorktreeToolAdapter:
    """Expose worktree creation through the policy-controlled tool boundary."""

    def __init__(self, manager: "GitWorktreeManager") -> None:
        self.manager = manager

    def execute(self, args: dict[str, object], workspace_root: Path) -> ToolExecutionOutput:
        change_id = args.get("change_id")
        run_id = args.get("run_id")
        base_ref = args.get("base_ref", "HEAD")
        if not isinstance(change_id, str) or not isinstance(run_id, str):
            raise ValueError("git_worktree_add requires change_id and run_id")
        if not isinstance(base_ref, str) or base_ref != "HEAD":
            raise ValueError("v0.1 worktrees must use HEAD as the base ref")
        handle = self.manager.create(change_id, run_id, base_ref)
        return ToolExecutionOutput(
            exit_code=0,
            stdout=json.dumps(
                {
                    "change_id": handle.change_id,
                    "run_id": handle.run_id,
                    "path": str(handle.path),
                    "branch": handle.branch,
                    "lock_path": str(handle.lock_path),
                },
                sort_keys=True,
            ),
        )


class GitWorktreeManager:
    def __init__(self, repo_path: str | Path, worktree_base: str | Path | None = None) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.worktree_base = Path(worktree_base).resolve() if worktree_base else self.repo_path.parent / ".vaf-worktrees"
        self.lock_base = self.repo_path / ".vaf" / "locks"

    def snapshot(self) -> RepoSnapshot:
        root = self._git("rev-parse", "--show-toplevel").strip()
        head = self._git("rev-parse", "HEAD").strip()
        branch_output = self._git("symbolic-ref", "--short", "-q", "HEAD", allow_failure=True).strip()
        changed = tuple(
            _status_path(line)
            for line in self._git("status", "--porcelain=v1").splitlines()
            if line.strip()
        )
        return RepoSnapshot(Path(root).resolve(), head, branch_output or None, changed)

    def create(self, change_id: str, run_id: str, base_ref: str = "HEAD") -> WorktreeHandle:
        snapshot = self.snapshot()
        unexpected = sorted(
            path
            for path in snapshot.changed_paths
            if path.rstrip("/") != ".vaf" and not path.startswith(".vaf/")
        )
        if unexpected:
            raise GitWorktreeError(
                f"repository has uncommitted paths outside VAF runtime state: {unexpected}"
            )
        safe_change = _safe_part(change_id)
        safe_run = _safe_part(run_id)
        lock_path = self.lock_base / f"{safe_change}.lock"
        self.lock_base.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise GitWorktreeError(f"change already has an active worktree lock: {change_id}") from exc
        else:
            os.close(fd)
        path = self.worktree_base / f"{safe_change}-{safe_run}"
        branch = f"vaf/{safe_change}/{safe_run}"
        try:
            self.worktree_base.mkdir(parents=True, exist_ok=True)
            self._git("worktree", "add", "-b", branch, str(path), base_ref)
        except Exception:
            lock_path.unlink(missing_ok=True)
            raise
        return WorktreeHandle(change_id, run_id, path, branch, lock_path)

    def changed_paths(self, handle: WorktreeHandle) -> tuple[str, ...]:
        output = self._git_at(handle.path, "status", "--porcelain=v1", "--untracked-files=all")
        return tuple(_status_path(line) for line in output.splitlines() if line.strip())

    @staticmethod
    def workspace_fingerprint(workspace_root: str | Path) -> str:
        """Hash tracked and non-ignored files without including VAF runtime logs."""

        root = Path(workspace_root).resolve()
        tracked = GitWorktreeManager._git_at(root, "ls-files", "--cached", "-z")
        untracked = GitWorktreeManager._git_at(root, "ls-files", "--others", "--exclude-standard", "-z")
        paths = {
            path
            for path in (*tracked.split("\0"), *untracked.split("\0"))
            if path
            and path != ".vaf"
            and not path.startswith(".vaf/")
            and "__pycache__" not in Path(path).parts
            and not path.endswith(".pyc")
        }
        digest = hashlib.sha256()
        for relative_path in sorted(paths):
            file_path = root / relative_path
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(str(file_path.stat().st_mode & 0o777).encode("ascii"))
                digest.update(b"\0")
                digest.update(file_path.read_bytes())
            except FileNotFoundError:
                digest.update(b"<missing>")
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    def assert_allowed_changes(self, handle: WorktreeHandle, allowed_paths: set[str]) -> None:
        changed = set(self.changed_paths(handle))
        unexpected = sorted(changed - allowed_paths)
        if unexpected:
            raise GitWorktreeError(f"worktree changed paths outside task scope: {unexpected}")

    def release(self, handle: WorktreeHandle, force: bool = False) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(handle.path))
        self._git(*args)
        handle.lock_path.unlink(missing_ok=True)

    def _git(self, *args: str, allow_failure: bool = False) -> str:
        return self._git_at(self.repo_path, *args, allow_failure=allow_failure)

    @staticmethod
    def _git_at(cwd: Path, *args: str, allow_failure: bool = False) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        if completed.returncode != 0 and not allow_failure:
            raise GitWorktreeError(completed.stderr.strip() or completed.stdout.strip() or "git command failed")
        return completed.stdout


def _safe_part(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not result:
        raise GitWorktreeError("identifier cannot produce an empty worktree name")
    return result


def _status_path(line: str) -> str:
    if len(line) < 4:
        return line.strip()
    return line[3:].strip().split(" -> ")[-1]
