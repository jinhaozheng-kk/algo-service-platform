"""SQLite 仓储：任务与产物的持久化。

选择 SQLite 而不是 MySQL/Redis，是为了让这个演示项目「克隆即跑」；
仓储接口（Repository）与具体存储解耦，换成 Postgres / Redis Queue 只需实现同样方法。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL UNIQUE,
    size        INTEGER NOT NULL,
    media_type  TEXT NOT NULL,
    filename    TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    capability      TEXT NOT NULL,
    version         TEXT NOT NULL,
    status          TEXT NOT NULL,
    artifact_id     TEXT,
    params          TEXT NOT NULL,
    result          TEXT,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT UNIQUE,
    request_id      TEXT,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    queue_wait_ms   REAL,
    run_ms          REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(idempotency_key);
"""

JOB_FIELDS = (
    "id", "capability", "version", "status", "artifact_id", "params", "result",
    "error", "attempts", "idempotency_key", "request_id", "created_at",
    "started_at", "finished_at", "queue_wait_ms", "run_ms",
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

    # ---------------- artifacts ----------------
    def create_artifact(self, *, sha256: str, size: int, media_type: str, path: str,
                        filename: str = "") -> dict[str, Any]:
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM artifacts WHERE sha256 = ?", (sha256,)
            ).fetchone()
            if existing:
                return dict(existing)
            artifact_id = new_id("art")
            self._conn.execute(
                "INSERT INTO artifacts (id, sha256, size, media_type, filename, path, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (artifact_id, sha256, size, media_type, filename, path, time.time()),
            )
            return dict(self._conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone())

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            return dict(row) if row else None

    # ---------------- jobs ----------------
    def create_job(
        self,
        *,
        capability: str,
        version: str,
        artifact_id: str | None,
        params: dict[str, Any],
        idempotency_key: str | None,
        request_id: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """返回 (job, created)。幂等键冲突时返回已存在任务，created=False。"""
        with self._lock, self._conn:
            if idempotency_key:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if row:
                    return dict(row), False
            job_id = new_id("job")
            try:
                self._conn.execute(
                    "INSERT INTO jobs (id, capability, version, status, artifact_id, params, "
                    "attempts, idempotency_key, request_id, created_at) "
                    "VALUES (?,?,?,?,?,?,0,?,?,?)",
                    (job_id, capability, version, "queued", artifact_id,
                     json.dumps(params, ensure_ascii=False), idempotency_key, request_id, time.time()),
                )
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if row:
                    return dict(row), False
                raise
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row), True

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self, *, status: str | None = None, capability: str | None = None,
                  limit: int = 20) -> list[dict[str, Any]]:
        clauses, args = [], []
        if status:
            clauses.append("status = ?")
            args.append(status)
        if capability:
            clauses.append("capability = ?")
            args.append(capability)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(max(1, min(limit, 200)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?", args
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_running(self, job_id: str) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1, "
                "queue_wait_ms=(? - created_at) * 1000 WHERE id = ?",
                (now, now, job_id),
            )

    def bump_attempt(self, job_id: str) -> None:
        """重试时累加执行次数，保证 attempts 反映真实执行次数。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET attempts=attempts+1 WHERE id = ?", (job_id,)
            )

    def finish_job(self, job_id: str, *, status: str, result: dict | None = None,
                   error: dict | None = None) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status=?, result=?, error=?, finished_at=?, "
                "run_ms=(? - started_at) * 1000 WHERE id = ?",
                (status,
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 json.dumps(error, ensure_ascii=False) if error is not None else None,
                 now, now, job_id),
            )

    def cancel_job(self, job_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE jobs SET status='canceled', finished_at=? WHERE id=? AND status='queued'",
                (time.time(), job_id),
            )
            return cur.rowcount > 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            totals = {
                row["status"]: row["n"]
                for row in self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
            }
            recent = [r["run_ms"] for r in self._conn.execute(
                "SELECT run_ms FROM jobs WHERE run_ms IS NOT NULL ORDER BY finished_at DESC LIMIT 2000"
            ) if r["run_ms"] is not None]
        recent.sort()
        p95 = recent[min(len(recent) - 1, int(round(0.95 * (len(recent) - 1))))] if recent else 0.0
        return {
            "totals": totals,
            "finished": len(recent),
            "run_ms": {
                "avg": round(sum(recent) / len(recent), 2) if recent else 0.0,
                "p95": round(p95, 2),
                "max": round(recent[-1], 2) if recent else 0.0,
            },
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._conn:
            yield self._conn
