from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from scholarrag.config import Settings
from scholarrag.evaluation.service import EvaluationCase, EvaluationRun
from scholarrag.rag.runs import (
    EventSink,
    LeaseLost,
    QueryCancellation,
    QueryRunRecorder,
    QueryRunStore,
    TraceEvent,
    utc_now,
)
from scholarrag.storage.scheduling import claim_fair_candidate

EvaluationJobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class EvaluationJob(BaseModel):
    schema_version: str = "1.0"
    job_id: str
    status: EvaluationJobStatus = "queued"
    fingerprint: str
    workspace_id: str | None = None
    owner_id: str | None = None
    actor_session_id: str | None = None
    execution_config: dict[str, Any] = Field(default_factory=dict)
    query_run_id: str | None = None
    case: EvaluationCase
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    event_count: int = 0
    result: EvaluationRun | None = None
    error: str | None = None
    artifact_dir: str


class EvaluationJobRecorder(EventSink):
    """Write metric events to the job stream and, when present, its parent query trace."""

    def __init__(
        self,
        store: EvaluationJobStore,
        job_id: str,
        parent: QueryRunRecorder | None = None,
    ) -> None:
        self.store = store
        self.job_id = job_id
        self.parent = parent

    async def emit(self, **payload: Any) -> TraceEvent:
        trace = dict(payload.get("trace") or {})
        trace["evaluation_job_id"] = self.job_id
        payload["trace"] = trace
        event = await self.store.append_event(self.job_id, **payload)
        if self.parent is not None:
            await self.parent.emit(**payload)
        return event


class EvaluationUncertain(RuntimeError):
    """A Judge call started without a durable result; do not replay it."""


class EvaluationJobStore:
    def __init__(
        self, settings: Settings, *, workspace_id: str | None = None, lease_token: str | None = None
    ) -> None:
        self.settings = settings
        self.database = QueryRunStore(settings, workspace_id=workspace_id)
        self.namespace = workspace_id or ""
        self.root = self.database.root.parent / "evaluation_jobs"
        self.lease_token = lease_token
        self.suspended = False
        with self.database.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS evaluation_job_snapshots (
                job_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner_id TEXT,
                fingerprint TEXT NOT NULL, idempotency_hash TEXT, query_run_id TEXT,
                status TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
                lease_token TEXT, lease_until REAL, worker_id TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                checkpoint TEXT, metrics TEXT NOT NULL DEFAULT '{}')""")
            connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS evaluation_idempotency
                ON evaluation_job_snapshots(namespace,owner_id,idempotency_hash)
                WHERE idempotency_hash IS NOT NULL""")
            connection.execute("""CREATE INDEX IF NOT EXISTS evaluation_queue
                ON evaluation_job_snapshots(status,lease_until,created_at)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS evaluation_job_events (
                job_id TEXT NOT NULL REFERENCES evaluation_job_snapshots(job_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(job_id,seq))""")

    @staticmethod
    def fingerprint(
        case: EvaluationCase,
        *,
        query_run_id: str | None,
        judge_model: str,
        judge_enable_thinking: bool,
        execution_config: dict | None = None,
    ) -> str:
        payload = {
            "case": case.model_dump(mode="json", exclude={"case_id"}),
            "query_run_id": query_run_id,
            "judge_model": judge_model,
            "judge_enable_thinking": judge_enable_thinking,
            "execution_config": execution_config,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _row(self, connection, job_id):
        row = connection.execute(
            "SELECT * FROM evaluation_job_snapshots WHERE job_id=? AND namespace=?",
            (job_id, self.namespace),
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return row

    def _assert_writer(self, row, *, allow_cancel=False):
        if self.suspended or (
            self.namespace
            and (
                not self.lease_token
                or row["lease_token"] != self.lease_token
                or (row["lease_until"] or 0) <= time.time()
            )
        ):
            raise LeaseLost("Evaluation lease unavailable")
        if row["cancel_requested"] and not allow_cancel:
            raise QueryCancellation()

    @staticmethod
    def _write(connection, job):
        connection.execute(
            "UPDATE evaluation_job_snapshots SET payload=?,status=? WHERE job_id=?",
            (job.model_dump_json(), job.status, job.job_id),
        )

    def _event(self, connection, job, **payload):
        event = TraceEvent(
            seq=job.event_count + 1, run_id=job.job_id, type=payload.pop("event_type"), **payload
        )
        connection.execute(
            "INSERT INTO evaluation_job_events VALUES(?,?,?)",
            (job.job_id, event.seq, event.model_dump_json()),
        )
        job.event_count = event.seq
        self._write(connection, job)
        return event

    async def create_or_get_active(
        self,
        *,
        case: EvaluationCase,
        query_run_id: str | None,
        fingerprint: str,
        owner_id: str | None = None,
        actor_session_id: str | None = None,
        execution_config: dict | None = None,
        idempotency_hash: str | None = None,
        authorize: Callable[[sqlite3.Connection], None] | None = None,
        max_active: int = 16,
        max_per_user: int = 4,
    ) -> tuple[EvaluationJob, bool]:
        from fastapi import HTTPException

        from scholarrag.domain.access import IdentityConflict

        with self.database.transaction() as connection:
            if authorize:
                authorize(connection)
            if self.namespace:
                if not owner_id or not actor_session_id or not query_run_id or not idempotency_hash:
                    raise ValueError(
                        "Workspace evaluation requires ownership, a parent run and idempotency"
                    )
                existing = connection.execute(
                    """SELECT * FROM evaluation_job_snapshots
                    WHERE namespace=? AND owner_id=? AND idempotency_hash=?""",
                    (self.namespace, owner_id, idempotency_hash),
                ).fetchone()
            else:
                existing = connection.execute(
                    """SELECT * FROM evaluation_job_snapshots
                    WHERE namespace='' AND fingerprint=? AND status IN ('queued','running')""",
                    (fingerprint,),
                ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise IdentityConflict(
                        "Idempotency key already used for a different evaluation"
                    )
                return EvaluationJob.model_validate_json(existing["payload"]), False
            if self.namespace:
                counts = connection.execute(
                    """SELECT COUNT(*),COALESCE(SUM(owner_id=?),0)
                    FROM evaluation_job_snapshots WHERE namespace=? AND status IN ('queued','running')""",
                    (owner_id, self.namespace),
                ).fetchone()
                if counts[0] >= max_active or counts[1] >= max_per_user:
                    raise HTTPException(status_code=429, detail="Too many active evaluations")
            job_id = f"evjob_{uuid.uuid4().hex}"
            job = EvaluationJob(
                job_id=job_id,
                fingerprint=fingerprint,
                query_run_id=query_run_id,
                case=case,
                workspace_id=self.namespace or None,
                owner_id=owner_id,
                actor_session_id=actor_session_id,
                execution_config=execution_config or {},
                artifact_dir=str(self.root / job_id),
            )
            connection.execute(
                """INSERT INTO evaluation_job_snapshots
                (job_id,namespace,owner_id,fingerprint,idempotency_hash,query_run_id,status,created_at,payload)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    self.namespace,
                    owner_id,
                    fingerprint,
                    idempotency_hash,
                    query_run_id,
                    job.status,
                    job.created_at.isoformat(),
                    job.model_dump_json(),
                ),
            )
            self._event(
                connection,
                job,
                event_type="evaluation.queued",
                stage_id="evaluation.request",
                label="Ragas 评测已排队",
                status="pending",
                summary="后台评测任务已创建",
                input={"query_run_id": query_run_id},
            )
            if self.namespace:
                from scholarrag.storage.identity import IdentityStore

                IdentityStore._audit(
                    connection, owner_id, self.namespace, "evaluation.created", job_id
                )
            return job, True

    def get(self, job_id):
        with self.database.transaction(write=False) as connection:
            try:
                return EvaluationJob.model_validate_json(self._row(connection, job_id)["payload"])
            except KeyError:
                return None

    def list(self, *, limit=100):
        with self.database.transaction(write=False) as connection:
            return [
                EvaluationJob.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT payload FROM evaluation_job_snapshots WHERE namespace=? ORDER BY created_at DESC LIMIT ?",
                    (self.namespace, limit),
                )
            ]

    def latest_for_query_run(self, query_run_id, *, owner_id: str | None = None):
        with self.database.transaction(write=False) as connection:
            row = connection.execute(
                """SELECT payload FROM evaluation_job_snapshots
                WHERE namespace=? AND query_run_id=? AND owner_id IS ? ORDER BY created_at DESC,job_id DESC LIMIT 1""",
                (self.namespace, query_run_id, owner_id),
            ).fetchone()
            return EvaluationJob.model_validate_json(row[0]) if row else None

    def list_events(self, job_id, *, after_seq=0):
        with self.database.transaction(write=False) as connection:
            try:
                self._row(connection, job_id)
            except KeyError:
                return []
            return [
                TraceEvent.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT payload FROM evaluation_job_events WHERE job_id=? AND seq>? ORDER BY seq",
                    (job_id, after_seq),
                )
            ]

    async def append_event(self, job_id, **payload):
        with self.database.transaction() as connection:
            row = self._row(connection, job_id)
            self._assert_writer(row)
            return self._event(
                connection, EvaluationJob.model_validate_json(row["payload"]), **payload
            )

    async def update(self, job_id, **changes):
        with self.database.transaction() as connection:
            row = self._row(connection, job_id)
            self._assert_writer(row)
            job = EvaluationJob.model_validate_json(row["payload"]).model_copy(update=changes)
            self._write(connection, job)
            return job

    async def start(self, job_id):
        with self.database.transaction() as connection:
            row = self._row(connection, job_id)
            self._assert_writer(row)
            job = EvaluationJob.model_validate_json(row["payload"])
            job.status = "running"
            job.started_at = job.started_at or utc_now()
            self._event(
                connection,
                job,
                event_type="evaluation.started",
                stage_id="evaluation.request",
                label="开始 Ragas 评测",
                status="running",
                summary="正在执行评测指标",
            )
            return job

    async def _finalize(self, job_id, status, *, result=None, error=None, authorize=None):
        with self.database.transaction() as connection:
            row = self._row(connection, job_id)
            self._assert_writer(row, allow_cancel=status == "cancelled")
            if authorize is not None:
                authorize(connection)
            job = EvaluationJob.model_validate_json(row["payload"])
            if job.status in {"completed", "failed", "cancelled"}:
                raise LeaseLost("Evaluation is already terminal")
            job.status = status
            job.completed_at = utc_now()
            job.result = result
            job.error = error
            self._event(
                connection,
                job,
                event_type=f"evaluation.{status}",
                stage_id="evaluation.persist",
                label={
                    "completed": "Ragas 评测完成",
                    "failed": "Ragas 评测失败",
                    "cancelled": "Ragas 评测已取消",
                }[status],
                status="success" if status == "completed" else status,
                summary="评测结果已持久化，原问答结果保持不变",
                error=error,
                output={"evaluation_run_id": result.run_id} if result else {},
            )
            connection.execute(
                "UPDATE evaluation_job_snapshots SET lease_token=NULL,lease_until=NULL WHERE job_id=?",
                (job_id,),
            )
            return job

    async def complete(self, job_id, result, *, authorize=None):
        return await self._finalize(job_id, "completed", result=result, authorize=authorize)

    async def fail(self, job_id, error):
        return await self._finalize(job_id, "failed", error=error)

    async def cancel(self, job_id, *, error="Cancelled by user"):
        return await self._finalize(job_id, "cancelled", error=error)

    async def request_cancel(self, job_id):
        with self.database.transaction() as connection:
            row = self._row(connection, job_id)
            job = EvaluationJob.model_validate_json(row["payload"])
            if job.status not in {"queued", "running"}:
                return job
            connection.execute(
                "UPDATE evaluation_job_snapshots SET cancel_requested=1 WHERE job_id=?", (job_id,)
            )
            if job.status == "queued":
                job.status = "cancelled"
                job.completed_at = utc_now()
                job.error = "Cancelled by user"
                self._event(
                    connection,
                    job,
                    event_type="evaluation.cancelled",
                    stage_id="evaluation.persist",
                    label="评测已取消",
                    status="cancelled",
                    summary="排队评测已取消",
                )
                connection.execute(
                    "UPDATE evaluation_job_snapshots SET lease_token=NULL,lease_until=NULL WHERE job_id=?",
                    (job_id,),
                )
            return job

    def claim_next(self, worker_id):
        with self.database.transaction() as connection:
            now = time.time()
            row = claim_fair_candidate(
                connection,
                "evaluation",
                """SELECT *,namespace AS workspace_id,job_id AS scheduling_id
                FROM evaluation_job_snapshots WHERE namespace<>''
                AND status IN ('queued','running') AND (lease_token IS NULL OR lease_until<=?)
                """,
                (now,),
            )
            if not row:
                return None
            token = uuid.uuid4().hex
            connection.execute(
                "UPDATE evaluation_job_snapshots SET lease_token=?,lease_until=?,worker_id=? WHERE job_id=?",
                (
                    token,
                    now + self.settings.evaluation_worker_lease_seconds,
                    worker_id,
                    row["job_id"],
                ),
            )
            return EvaluationJob.model_validate_json(row["payload"]), token

    def check_execution(self, job_id):
        with self.database.transaction(write=False) as connection:
            self._assert_writer(self._row(connection, job_id))

    def heartbeat(self, job_id):
        with self.database.transaction() as connection:
            row = self._row(connection, job_id)
            self._assert_writer(row, allow_cancel=True)
            connection.execute(
                "UPDATE evaluation_job_snapshots SET lease_until=? WHERE job_id=?",
                (time.time() + self.settings.evaluation_worker_lease_seconds, job_id),
            )
            return bool(row["cancel_requested"])

    def save_result(self, job_id, result):
        with self.database.transaction() as connection:
            row = self._row(connection, job_id)
            self._assert_writer(row)
            connection.execute(
                "UPDATE evaluation_job_snapshots SET checkpoint=? WHERE job_id=?",
                (result.model_dump_json(), job_id),
            )

    def result_checkpoint(self, job_id):
        with self.database.transaction(write=False) as connection:
            value = self._row(connection, job_id)["checkpoint"]
            return EvaluationRun.model_validate_json(value) if value else None

    def metrics(self, job_id):
        with self.database.transaction(write=False) as connection:
            self._assert_writer(self._row(connection, job_id))
            return json.loads(self._row(connection, job_id)["metrics"])

    def save_metric(self, job_id, key, value):
        with self.database.transaction() as connection:
            row = self._row(connection, job_id)
            self._assert_writer(row)
            metrics = json.loads(row["metrics"])
            metrics[key] = value
            connection.execute(
                "UPDATE evaluation_job_snapshots SET metrics=? WHERE job_id=?",
                (json.dumps(metrics), job_id),
            )

    async def wait_for_events(self, job_id, *, after_seq, timeout=15.0):
        deadline = time.monotonic() + timeout
        while True:
            events = self.list_events(job_id, after_seq=after_seq)
            job = self.get(job_id)
            if events or job is None or job.status in {"completed", "failed", "cancelled"}:
                return events
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            await asyncio.sleep(min(0.1, remaining))

    async def recover_interrupted(self):
        recovered = []
        for job in self.list(limit=10000):
            if job.status in {"queued", "running"}:
                await self.fail(job.job_id, "Backend restarted before this evaluation completed")
                recovered.append(job.job_id)
        return recovered

    def recorder(self, job_id, *, parent=None):
        if self.get(job_id) is None:
            raise KeyError(job_id)
        return EvaluationJobRecorder(self, job_id, parent)


class EvaluationMetricJournal:
    def __init__(self, store: EvaluationJobStore, job_id: str):
        self.store = store
        self.job_id = job_id

    def initialize(self):
        self.store.save_metric(self.job_id, "__initialized", {"state": "done"})

    def get(self, name):
        from scholarrag.evaluation.service import MetricScore

        record = self.store.metrics(self.job_id).get(name)
        if not record:
            return None
        if record["state"] != "done":
            raise EvaluationUncertain("Judge outcome unknown; metric was not replayed")
        return MetricScore.model_validate(record["score"])

    def begin(self, name):
        self.store.save_metric(self.job_id, name, {"state": "running"})

    def finish(self, name, score):
        self.store.save_metric(
            self.job_id, name, {"state": "done", "score": score.model_dump(mode="json")}
        )
