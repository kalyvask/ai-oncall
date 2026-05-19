"""SQLite-backed job queue. Single-process worker, single-VM deployment.

Schema is deliberately small. Idempotency on ``(kind, tenant_id, idempotency_key)``
means a duplicate alert webhook returns the existing job rather than creating
a second one. Status lifecycle:

    pending -> running -> done
                       -> failed (after max_attempts)

Retries: ``fail(retry=True)`` increments attempts and sets ``next_attempt_at``
with exponential backoff (1s, 2s, 4s, 8s...). The worker claims the oldest
``pending`` job whose ``next_attempt_at <= now``.

The DB path is module-level so tests can monkeypatch it; defaults to
``data/jobs.sqlite``.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from pydantic import BaseModel

JOBS_DB_PATH = Path("data/jobs.sqlite")

JobKind = Literal["rca", "slack_post"]
JobStatus = Literal["pending", "running", "done", "failed"]


class JobRecord(BaseModel):
    job_id: str
    kind: JobKind
    tenant_id: str
    idempotency_key: str
    status: JobStatus
    payload_json: str
    result_json: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime

    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    def result(self) -> Optional[dict[str, Any]]:
        return json.loads(self.result_json) if self.result_json else None


def _resolve_db_path(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    import ai_oncall.jobs.store as _self

    return _self.JOBS_DB_PATH


@contextmanager
def _conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    resolved = _resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            next_attempt_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (kind, tenant_id, idempotency_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_next ON jobs(status, next_attempt_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created ON jobs(tenant_id, created_at)"
    )


def _row_to_record(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        kind=row["kind"],
        tenant_id=row["tenant_id"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        payload_json=row["payload_json"],
        result_json=row["result_json"],
        error=row["error"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        next_attempt_at=datetime.fromisoformat(row["next_attempt_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(
    *,
    kind: JobKind,
    tenant_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    max_attempts: int = 3,
    db_path: Path | None = None,
) -> JobRecord:
    """Insert a job or return the existing one for this idempotency key.

    The unique constraint on (kind, tenant_id, idempotency_key) means the
    second call with the same key is a no-op and returns the first job.
    """
    now = _now()
    job_id = str(uuid.uuid4())
    payload_json = json.dumps(payload, default=str)
    with _conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO jobs (
                job_id, kind, tenant_id, idempotency_key, status,
                payload_json, attempts, max_attempts, next_attempt_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?, ?)
            """,
            (
                job_id,
                kind,
                tenant_id,
                idempotency_key,
                payload_json,
                max_attempts,
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        cur = conn.execute(
            "SELECT * FROM jobs WHERE kind = ? AND tenant_id = ? AND idempotency_key = ?",
            (kind, tenant_id, idempotency_key),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("enqueue: row vanished after insert")
        return _row_to_record(row)


def claim_next(*, db_path: Path | None = None) -> Optional[JobRecord]:
    """Atomically claim the oldest pending job that's eligible to run now."""
    now = _now()
    with _conn(db_path) as conn:
        cur = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'pending' AND next_attempt_at <= ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (now.isoformat(),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        job_id = row["job_id"]
        conn.execute(
            "UPDATE jobs SET status='running', updated_at=?, attempts=attempts+1 WHERE job_id=?",
            (now.isoformat(), job_id),
        )
        cur = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        return _row_to_record(cur.fetchone())


def complete(
    job_id: str,
    *,
    result: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> None:
    now = _now()
    result_json = json.dumps(result, default=str) if result is not None else None
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status='done', result_json=?, error=NULL, updated_at=? WHERE job_id=?",
            (result_json, now.isoformat(), job_id),
        )


def fail(
    job_id: str,
    *,
    error: str,
    retry: bool = True,
    delay_seconds: float | None = None,
    db_path: Path | None = None,
) -> JobRecord:
    """Mark a running job failed. If retry is True and attempts<max_attempts,
    flip back to pending with backoff. ``delay_seconds`` overrides the
    default exponential backoff — useful for honoring upstream Retry-After
    headers."""
    now = _now()
    with _conn(db_path) as conn:
        cur = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"job not found: {job_id}")
        attempts = row["attempts"]
        max_attempts = row["max_attempts"]
        if retry and attempts < max_attempts:
            if delay_seconds is not None:
                backoff_seconds = max(0.0, min(300.0, delay_seconds))
            else:
                backoff_seconds = min(60.0, math.pow(2, attempts))
            next_at = (now + timedelta(seconds=backoff_seconds)).isoformat()
            conn.execute(
                "UPDATE jobs SET status='pending', error=?, next_attempt_at=?, updated_at=? WHERE job_id=?",
                (error[:2000], next_at, now.isoformat(), job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, updated_at=? WHERE job_id=?",
                (error[:2000], now.isoformat(), job_id),
            )
        cur = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        return _row_to_record(cur.fetchone())


def get_job(job_id: str, *, db_path: Path | None = None) -> Optional[JobRecord]:
    with _conn(db_path) as conn:
        cur = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        row = cur.fetchone()
        return _row_to_record(row) if row else None


def list_jobs(
    *,
    tenant_id: str,
    status: JobStatus | None = None,
    limit: int = 50,
    db_path: Path | None = None,
) -> list[JobRecord]:
    where = ["tenant_id = ?"]
    params: list[object] = [tenant_id]
    if status:
        where.append("status = ?")
        params.append(status)
    params.append(limit)
    with _conn(db_path) as conn:
        cur = conn.execute(
            f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        return [_row_to_record(r) for r in cur.fetchall()]
