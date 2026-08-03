from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from scholarrag.config import Settings

JobState = Literal["queued", "parsing", "ready", "failed"]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class IngestionJob(BaseModel):
    job_id: str
    status: JobState
    filename: str
    source_path: str
    idempotency_key: str
    attempts: int = 0
    max_attempts: int = 3
    document_id: str | None = None
    parse_id: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class IngestionJobStore:
    """SQLite-backed recoverable ingestion queue with immutable event history."""

    def __init__(self, settings: Settings) -> None:
        self.path = settings.sqlite_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    document_id TEXT,
                    parse_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingestion_job_events (
                    job_id TEXT NOT NULL REFERENCES ingestion_jobs(job_id),
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    message TEXT,
                    PRIMARY KEY(job_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_status
                ON ingestion_jobs(status, created_at);
                """
            )

    def enqueue(
        self,
        *,
        filename: str,
        source_path: Path,
        idempotency_key: str,
        max_attempts: int = 3,
    ) -> IngestionJob:
        timestamp = now_iso()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM ingestion_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return self._row(existing)
            job_id = f"job_{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO ingestion_jobs(
                    job_id,status,filename,source_path,idempotency_key,attempts,
                    max_attempts,created_at,updated_at
                ) VALUES(?,?,?,?,?,0,?,?,?)
                """,
                (
                    job_id,
                    "queued",
                    filename,
                    str(source_path),
                    idempotency_key,
                    max_attempts,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_event(connection, job_id, "job.queued", "queued", "任务已入队")
            row = connection.execute(
                "SELECT * FROM ingestion_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create ingestion job")
        return self._row(row)

    def get(self, job_id: str) -> IngestionJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ingestion_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._row(row) if row else None

    def list(self, limit: int = 100) -> list[IngestionJob]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ingestion_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def claim_next(self) -> IngestionJob | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM ingestion_jobs WHERE status='queued'
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            timestamp = now_iso()
            connection.execute(
                """
                UPDATE ingestion_jobs
                SET status='parsing', attempts=attempts+1, updated_at=?, error=NULL
                WHERE job_id=? AND status='queued'
                """,
                (timestamp, row["job_id"]),
            )
            self._append_event(
                connection,
                row["job_id"],
                "job.started",
                "parsing",
                f"开始第 {int(row['attempts']) + 1} 次执行",
            )
            claimed = connection.execute(
                "SELECT * FROM ingestion_jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._row(claimed) if claimed else None

    def complete(self, job_id: str, *, document_id: str, parse_id: str) -> IngestionJob:
        return self._transition(
            job_id,
            status="ready",
            event_type="job.completed",
            message="解析与索引完成",
            document_id=document_id,
            parse_id=parse_id,
            error=None,
        )

    def fail(self, job_id: str, error: str) -> IngestionJob:
        current = self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        retry = current.attempts < current.max_attempts
        return self._transition(
            job_id,
            status="queued" if retry else "failed",
            event_type="job.retry_scheduled" if retry else "job.dead_lettered",
            message=(f"执行失败，将自动重试：{error}" if retry else f"达到最大重试次数：{error}"),
            error=error,
        )

    def retry(self, job_id: str) -> IngestionJob:
        current = self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        if current.status != "failed":
            raise ValueError("Only failed jobs can be retried")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ingestion_jobs
                SET status='queued', attempts=0, error=NULL, updated_at=? WHERE job_id=?
                """,
                (now_iso(), job_id),
            )
            self._append_event(connection, job_id, "job.manual_retry", "queued", "用户手动重试")
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def recover_interrupted(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM ingestion_jobs WHERE status='parsing'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE ingestion_jobs SET status='queued', updated_at=? WHERE job_id=?",
                    (now_iso(), row["job_id"]),
                )
                self._append_event(
                    connection,
                    row["job_id"],
                    "job.recovered",
                    "queued",
                    "进程重启后恢复到队列",
                )
        return [str(row["job_id"]) for row in rows]

    def events(self, job_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT seq,event_type,status,timestamp,message
                FROM ingestion_job_events WHERE job_id=? ORDER BY seq
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _transition(
        self,
        job_id: str,
        *,
        status: JobState,
        event_type: str,
        message: str,
        document_id: str | None = None,
        parse_id: str | None = None,
        error: str | None = None,
    ) -> IngestionJob:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_jobs SET status=?, updated_at=?, document_id=COALESCE(?,document_id),
                parse_id=COALESCE(?,parse_id), error=? WHERE job_id=?
                """,
                (status, now_iso(), document_id, parse_id, error, job_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(job_id)
            self._append_event(connection, job_id, event_type, status, message)
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        status: str,
        message: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO ingestion_job_events(job_id,seq,event_type,status,timestamp,message)
            SELECT ?,COALESCE(MAX(seq),0)+1,?,?,?,?
            FROM ingestion_job_events WHERE job_id=?
            """,
            (job_id, event_type, status, now_iso(), message, job_id),
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> IngestionJob:
        return IngestionJob.model_validate(dict(row))
