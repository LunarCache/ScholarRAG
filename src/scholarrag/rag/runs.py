from __future__ import annotations

import asyncio
import re
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from scholarrag.config import Settings
from scholarrag.domain.models import Answer
from scholarrag.observability import observe
from scholarrag.storage.scheduling import claim_fair_candidate, initialize_scheduling

RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
StageStatus = Literal["pending", "running", "success", "skipped", "warning", "failed", "cancelled"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class TraceEvent(BaseModel):
    schema_version: str = "1.0"
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    seq: int
    run_id: str
    type: str
    stage_id: str
    parent_stage_id: str | None = None
    label: str
    status: StageStatus
    timestamp: datetime = Field(default_factory=utc_now)
    duration_ms: float | None = None
    component: str | None = None
    model: str | None = None
    summary: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    trace: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class QueryRun(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    trace_id: str
    status: RunStatus = "queued"
    question: str
    document_ids: list[str] | None = None
    top_k: int = 8
    agentic: bool = True
    tool_mode: bool | None = None
    conversation_id: str | None = None
    parent_run_id: str | None = None
    workspace_id: str | None = None
    owner_id: str | None = None
    actor_session_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    event_count: int = 0
    answer: Answer | None = None
    error: str | None = None
    artifact_dir: str


class EventSink(Protocol):
    async def emit(
        self,
        *,
        event_type: str,
        stage_id: str,
        label: str,
        status: StageStatus,
        parent_stage_id: str | None = None,
        duration_ms: float | None = None,
        component: str | None = None,
        model: str | None = None,
        summary: str | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        trace: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> TraceEvent: ...


class QueryRunRecorder:
    def __init__(self, store: QueryRunStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id

    async def emit(
        self,
        *,
        event_type: str,
        stage_id: str,
        label: str,
        status: StageStatus,
        parent_stage_id: str | None = None,
        duration_ms: float | None = None,
        component: str | None = None,
        model: str | None = None,
        summary: str | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        trace: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> TraceEvent:
        return await self.store.append_event(
            self.run_id,
            event_type=event_type,
            stage_id=stage_id,
            label=label,
            status=status,
            parent_stage_id=parent_stage_id,
            duration_ms=duration_ms,
            component=component,
            model=model,
            summary=summary,
            input=input or {},
            output=output or {},
            trace=trace or {},
            error=error,
        )


class StageCapture:
    def __init__(self) -> None:
        self.output: dict[str, Any] = {}
        self.trace: dict[str, Any] = {}
        self.summary: str | None = None
        self.status: StageStatus = "success"

    def complete(
        self,
        *,
        output: dict[str, Any] | None = None,
        trace: dict[str, Any] | None = None,
        summary: str | None = None,
        status: StageStatus = "success",
    ) -> None:
        self.output = output or {}
        self.trace = trace or {}
        self.summary = summary
        self.status = status


class PipelineTracer:
    """Small event facade shared by synchronous RAG and persisted query runs."""

    def __init__(self, sink: EventSink | None = None) -> None:
        self.sink = sink

    @asynccontextmanager
    async def stage(
        self,
        stage_id: str,
        label: str,
        *,
        parent_stage_id: str | None = None,
        component: str | None = None,
        model: str | None = None,
        input: dict[str, Any] | None = None,
    ) -> AsyncIterator[StageCapture]:
        with observe(
            stage_id,
            input=input,
            metadata={"component": component, "label": label, "parent_stage_id": parent_stage_id},
        ) as observation:
            async with self._stage(
                stage_id,
                label,
                parent_stage_id=parent_stage_id,
                component=component,
                model=model,
                input=input,
            ) as capture:
                yield capture
                observation.update(
                    output=capture.output,
                    level="WARNING" if capture.status == "warning" else "DEFAULT",
                )

    @asynccontextmanager
    async def _stage(
        self,
        stage_id: str,
        label: str,
        *,
        parent_stage_id: str | None = None,
        component: str | None = None,
        model: str | None = None,
        input: dict[str, Any] | None = None,
    ) -> AsyncIterator[StageCapture]:
        started = time.perf_counter()
        if self.sink:
            await self.sink.emit(
                event_type="stage.started",
                stage_id=stage_id,
                label=label,
                status="running",
                parent_stage_id=parent_stage_id,
                component=component,
                model=model,
                input=input,
            )
        capture = StageCapture()
        try:
            yield capture
        except asyncio.CancelledError:
            if self.sink:
                await self.sink.emit(
                    event_type="stage.cancelled",
                    stage_id=stage_id,
                    label=label,
                    status="cancelled",
                    parent_stage_id=parent_stage_id,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    component=component,
                    model=model,
                    input=input,
                    error="Cancelled by user",
                )
            raise
        except Exception as exc:
            if self.sink:
                await self.sink.emit(
                    event_type="stage.failed",
                    stage_id=stage_id,
                    label=label,
                    status="failed",
                    parent_stage_id=parent_stage_id,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    component=component,
                    model=model,
                    input=input,
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        else:
            if self.sink:
                await self.sink.emit(
                    event_type="stage.completed",
                    stage_id=stage_id,
                    label=label,
                    status=capture.status,
                    parent_stage_id=parent_stage_id,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    component=component,
                    model=model,
                    summary=capture.summary,
                    input=input,
                    output=capture.output,
                    trace=capture.trace,
                )

    async def skip(
        self,
        stage_id: str,
        label: str,
        *,
        summary: str,
        parent_stage_id: str | None = None,
        component: str | None = None,
        model: str | None = None,
    ) -> None:
        if self.sink:
            await self.sink.emit(
                event_type="stage.skipped",
                stage_id=stage_id,
                label=label,
                status="skipped",
                parent_stage_id=parent_stage_id,
                component=component,
                model=model,
                summary=summary,
            )


class LeaseLost(RuntimeError):
    """This executor can no longer mutate its run."""


class QueryCancellation(asyncio.CancelledError):
    """Persistent cancellation observed at a write boundary."""


class QueryRunStore:
    """SQLite snapshots and ordered events committed in the same transaction.

    Workspace executors must hold a live lease. Local developer-mode queries
    use the same storage, but are still executed by the API process.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        workspace_id: str | None = None,
        lease_token: str | None = None,
    ) -> None:
        if workspace_id is not None and not re.fullmatch(r"ws_[0-9a-f]{32}", workspace_id):
            raise ValueError("Invalid workspace storage namespace")
        self.settings = settings
        self.namespace = workspace_id or ""
        self.lease_token = lease_token
        self.suspended = False
        root = (
            settings.data_dir
            if workspace_id is None
            else settings.data_dir / "workspaces" / workspace_id
        )
        self.root = root / "query_runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = settings.sqlite_path
        with self.transaction() as connection:
            initialize_scheduling(connection)
            connection.execute("""CREATE TABLE IF NOT EXISTS query_run_snapshots (
                run_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner_id TEXT,
                status TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
                lease_token TEXT, lease_until REAL, worker_id TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0, checkpoint TEXT)""")
            connection.execute("""CREATE INDEX IF NOT EXISTS query_run_queue
                ON query_run_snapshots(namespace,status,created_at)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS query_run_events (
                run_id TEXT NOT NULL REFERENCES query_run_snapshots(run_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,seq))""")

    @contextmanager
    def transaction(self, *, write: bool = True):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _row(self, connection, run_id: str):
        row = connection.execute(
            "SELECT * FROM query_run_snapshots WHERE run_id=? AND namespace=?",
            (run_id, self.namespace),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    def _assert_writer(self, row, *, allow_cancel: bool = False) -> None:
        if self.suspended or (
            self.namespace
            and (
                not self.lease_token
                or row["lease_token"] != self.lease_token
                or (row["lease_until"] or 0) <= time.time()
            )
        ):
            raise LeaseLost("Query lease unavailable")
        if row["cancel_requested"] and not allow_cancel:
            raise QueryCancellation()

    @staticmethod
    def _write(connection, run: QueryRun) -> None:
        connection.execute(
            "UPDATE query_run_snapshots SET status=?,payload=? WHERE run_id=?",
            (run.status, run.model_dump_json(), run.run_id),
        )

    def _event(self, connection, run: QueryRun, **payload: Any) -> TraceEvent:
        event = TraceEvent(
            seq=run.event_count + 1, run_id=run.run_id, type=payload.pop("event_type"), **payload
        )
        connection.execute(
            "INSERT INTO query_run_events VALUES(?,?,?)",
            (run.run_id, event.seq, event.model_dump_json()),
        )
        run.event_count = event.seq
        self._write(connection, run)
        return event

    async def create(
        self,
        *,
        question: str,
        document_ids: list[str] | None,
        top_k: int,
        agentic: bool = True,
        tool_mode: bool | None = None,
        conversation_id: str | None = None,
        parent_run_id: str | None = None,
        workspace_id: str | None = None,
        owner_id: str | None = None,
        actor_session_id: str | None = None,
        register: Callable[[sqlite3.Connection, QueryRun], None] | None = None,
    ) -> QueryRun:
        if (workspace_id or "") != self.namespace:
            raise ValueError("Query workspace does not match store")
        run_id = f"run_{uuid.uuid4().hex}"
        run = QueryRun(
            run_id=run_id,
            trace_id=uuid.uuid4().hex,
            question=question,
            document_ids=document_ids,
            top_k=top_k,
            agentic=agentic,
            tool_mode=tool_mode,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            actor_session_id=actor_session_id,
            artifact_dir=str(self.root / run_id),
        )
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO query_run_snapshots
                (run_id,namespace,owner_id,status,created_at,payload) VALUES(?,?,?,?,?,?)""",
                (
                    run_id,
                    self.namespace,
                    owner_id,
                    run.status,
                    run.created_at.isoformat(),
                    run.model_dump_json(),
                ),
            )
            if register is not None:
                register(connection, run)
            self._event(
                connection,
                run,
                event_type="run.created",
                stage_id="request",
                label="接收问题",
                status="success",
                summary="问答任务已创建",
                input={
                    "document_ids": document_ids or [],
                    "top_k": top_k,
                    "agentic": agentic,
                    "tool_mode": tool_mode,
                    "conversation_id": conversation_id,
                    "parent_run_id": parent_run_id,
                },
            )
        return run

    def get(self, run_id: str) -> QueryRun | None:
        with self.transaction(write=False) as connection:
            try:
                return QueryRun.model_validate_json(self._row(connection, run_id)["payload"])
            except KeyError:
                return None

    def list(self, *, limit: int = 50) -> list[QueryRun]:
        with self.transaction(write=False) as connection:
            return [
                QueryRun.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT payload FROM query_run_snapshots WHERE namespace=? ORDER BY created_at DESC LIMIT ?",
                    (self.namespace, limit),
                )
            ]

    async def recover_interrupted(self) -> list[str]:
        recovered: list[str] = []
        for run in self.list(limit=10_000):
            if run.status not in {"queued", "running"}:
                continue
            await self.finalize(
                run.run_id,
                status="failed",
                completed_at=utc_now(),
                error="Backend restarted before this query run completed",
                event_type="run.failed",
                stage_id="response.persist",
                label="后端重启，任务中断",
                event_status="failed",
                summary="检测到未完成的历史任务，已标记为失败并保留已有事件",
            )
            recovered.append(run.run_id)
        return recovered

    async def recover_interrupted_child_stages(self) -> list[str]:
        """Close evaluation stages interrupted after their parent run completed."""
        recovered: list[str] = []
        for run in self.list(limit=10_000):
            if run.status not in {"completed", "failed", "cancelled"}:
                continue
            latest: dict[str, TraceEvent] = {}
            for event in self.list_events(run.run_id):
                latest[event.stage_id] = event
            for stage_id, event in latest.items():
                if not stage_id.startswith("evaluation.") or event.status != "running":
                    continue
                await self.append_event(
                    run.run_id,
                    event_type="evaluation.interrupted",
                    stage_id=stage_id,
                    label=event.label,
                    status="warning",
                    parent_stage_id=event.parent_stage_id,
                    component=event.component,
                    model=event.model,
                    summary="后端重启中断了该评测指标；父问答结果保持不变，可重新评测",
                    error="Backend restarted before this evaluation metric completed",
                )
                recovered.append(f"{run.run_id}:{stage_id}")
        return recovered

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[TraceEvent]:
        with self.transaction(write=False) as connection:
            try:
                self._row(connection, run_id)
            except KeyError:
                return []
            return [
                TraceEvent.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT payload FROM query_run_events WHERE run_id=? AND seq>? ORDER BY seq",
                    (run_id, after_seq),
                )
            ]

    async def append_event(self, run_id: str, **payload: Any) -> TraceEvent:
        with self.transaction() as connection:
            row = self._row(connection, run_id)
            self._assert_writer(row)
            run = QueryRun.model_validate_json(row["payload"])
            return self._event(connection, run, **payload)

    async def update(self, run_id: str, **changes: Any) -> QueryRun:
        with self.transaction() as connection:
            row = self._row(connection, run_id)
            self._assert_writer(row)
            run = QueryRun.model_validate_json(row["payload"]).model_copy(update=changes)
            self._write(connection, run)
            return run

    async def finalize(
        self,
        run_id: str,
        *,
        status: Literal["completed", "failed", "cancelled"],
        completed_at: datetime,
        answer: Answer | None = None,
        error: str | None = None,
        event_type: str,
        stage_id: str,
        label: str,
        event_status: StageStatus,
        summary: str,
        output: dict[str, Any] | None = None,
        trace: dict[str, Any] | None = None,
    ) -> tuple[QueryRun, TraceEvent]:
        with self.transaction() as connection:
            row = self._row(connection, run_id)
            self._assert_writer(row, allow_cancel=status == "cancelled")
            run = QueryRun.model_validate_json(row["payload"])
            if run.status in {"completed", "failed", "cancelled"}:
                raise LeaseLost("Query is already terminal")
            run = run.model_copy(
                update={
                    "status": status,
                    "completed_at": completed_at,
                    "answer": answer,
                    "error": error,
                }
            )
            event = self._event(
                connection,
                run,
                event_type=event_type,
                stage_id=stage_id,
                label=label,
                status=event_status,
                summary=summary,
                output=output or {},
                trace=trace or {},
                error=error,
            )
            connection.execute(
                "UPDATE query_run_snapshots SET lease_token=NULL,lease_until=NULL WHERE run_id=?",
                (run_id,),
            )
            return run, event

    async def save_answer_checkpoint(self, run_id: str, answer: Answer) -> None:
        with self.transaction() as connection:
            row = self._row(connection, run_id)
            self._assert_writer(row)
            connection.execute(
                "UPDATE query_run_snapshots SET checkpoint=? WHERE run_id=?",
                (answer.model_dump_json(), run_id),
            )

    def checkpoint(self, run_id: str) -> Answer | None:
        with self.transaction(write=False) as connection:
            value = self._row(connection, run_id)["checkpoint"]
            return Answer.model_validate_json(value) if value else None

    def claim_next(self, worker_id: str) -> tuple[QueryRun, str] | None:
        """Atomically acquire queued or expired work; only registered workspace runs."""
        with self.transaction() as connection:
            now = time.time()
            row = claim_fair_candidate(
                connection,
                "query",
                """SELECT q.*,q.namespace AS workspace_id,q.run_id AS scheduling_id
                FROM query_run_snapshots q
                JOIN auth_query_runs a ON a.run_id=q.run_id AND a.workspace_id=q.namespace
                WHERE q.namespace<>'' AND (q.status='queued' OR
                    (q.status='running' AND COALESCE(q.lease_until,0)<=?))
                AND (q.lease_token IS NULL OR COALESCE(q.lease_until,0)<=?)
                """,
                (now, now),
            )
            if row is None:
                return None
            token = uuid.uuid4().hex
            connection.execute(
                """UPDATE query_run_snapshots SET lease_token=?,lease_until=?,worker_id=?
                WHERE run_id=?""",
                (token, now + self.settings.query_worker_lease_seconds, worker_id, row["run_id"]),
            )
            return QueryRun.model_validate_json(row["payload"]), token

    def check_execution(self, run_id: str) -> None:
        with self.transaction(write=False) as connection:
            self._assert_writer(self._row(connection, run_id))

    def heartbeat(self, run_id: str) -> bool:
        with self.transaction() as connection:
            row = self._row(connection, run_id)
            self._assert_writer(row, allow_cancel=True)
            connection.execute(
                "UPDATE query_run_snapshots SET lease_until=? WHERE run_id=?",
                (time.time() + self.settings.query_worker_lease_seconds, run_id),
            )
            return bool(row["cancel_requested"])

    async def request_cancel(self, run_id: str) -> QueryRun:
        with self.transaction() as connection:
            row = self._row(connection, run_id)
            run = QueryRun.model_validate_json(row["payload"])
            if run.status not in {"queued", "running"}:
                return run
            connection.execute(
                "UPDATE query_run_snapshots SET cancel_requested=1 WHERE run_id=?", (run_id,)
            )
            # A queued job has made no external call. Its lease (if just acquired)
            # is invalidated here so it cannot start after cancellation commits.
            if run.status == "queued":
                run.status = "cancelled"
                run.completed_at = utc_now()
                run.error = "Cancelled by user"
                self._event(
                    connection,
                    run,
                    event_type="run.cancelled",
                    stage_id="response.persist",
                    label="回答已取消",
                    status="cancelled",
                    summary="用户取消了排队问答",
                )
                connection.execute(
                    "UPDATE query_run_snapshots SET lease_token=NULL,lease_until=NULL WHERE run_id=?",
                    (run_id,),
                )
            return run

    async def wait_for_events(
        self, run_id: str, *, after_seq: int, timeout: float = 15.0
    ) -> list[TraceEvent]:
        deadline = time.monotonic() + timeout
        while True:
            events = self.list_events(run_id, after_seq=after_seq)
            run = self.get(run_id)
            if events or run is None or run.status in {"completed", "failed", "cancelled"}:
                return events
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            await asyncio.sleep(min(0.1, remaining))

    def recorder(self, run_id: str) -> QueryRunRecorder:
        if self.get(run_id) is None:
            raise KeyError(run_id)
        return QueryRunRecorder(self, run_id)
