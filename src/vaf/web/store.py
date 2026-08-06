"""Small SQLite job store for the local Web control plane."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


class JobStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    project_path TEXT,
                    generated_path TEXT,
                    stack_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                )
                """
            )

    def create(self, job: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "job_id": job["job_id"],
            "title": job["title"],
            "source_type": job["source_type"],
            "source_ref": job["source_ref"],
            "source_hash": job["source_hash"],
            "status": "QUEUED",
            "phase": "queued",
            "created_at": now,
            "updated_at": now,
            "project_path": job.get("project_path"),
            "generated_path": None,
            "stack_json": json.dumps(job.get("stack", {}), ensure_ascii=False),
            "result_json": "{}",
            "error": None,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, title, source_type, source_ref, source_hash, status,
                    phase, created_at, updated_at, project_path, generated_path,
                    stack_json, result_json, error
                ) VALUES (:job_id, :title, :source_type, :source_ref, :source_hash,
                    :status, :phase, :created_at, :updated_at, :project_path,
                    :generated_path, :stack_json, :result_json, :error)
                """,
                record,
            )
        return self.get(str(job["job_id"])) or record

    def update(self, job_id: str, **values: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "phase",
            "project_path",
            "generated_path",
            "stack_json",
            "result_json",
            "error",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if "stack" in values:
            updates["stack_json"] = json.dumps(values["stack"], ensure_ascii=False)
        if "result" in values:
            updates["result_json"] = json.dumps(values["result"], ensure_ascii=False)
        if not updates:
            return self.get(job_id) or {}
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ", ".join(f"{key} = :{key}" for key in updates)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id = :job_id",
                {**updates, "job_id": job_id},
            )
        return self.get(job_id) or {}

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),)
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["stack"] = json.loads(record.pop("stack_json") or "{}")
    record["result"] = json.loads(record.pop("result_json") or "{}")
    return record
