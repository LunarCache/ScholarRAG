from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from scholarrag.config import Settings
from scholarrag.domain.models import Answer
from scholarrag.observability import observe

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


class QueryRunStore:
    """Append-only event artifacts plus recoverable run snapshots."""

    def __init__(self, settings: Settings, *, workspace_id: str | None = None) -> None:
        if workspace_id is not None and not re.fullmatch(r"ws_[0-9a-f]{32}", workspace_id):
            raise ValueError("Invalid workspace storage namespace")
        root = (
            settings.data_dir
            if workspace_id is None
            else settings.data_dir / "workspaces" / workspace_id
        )
        self.root = root / "query_runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._conditions: dict[str, asyncio.Condition] = {}

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
    ) -> QueryRun:
        run_id = f"run_{uuid.uuid4().hex}"
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
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
            artifact_dir=str(run_dir),
        )
        self._write_run(run)
        await self.append_event(
            run_id,
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
        return self.get(run_id) or run

    def get(self, run_id: str) -> QueryRun | None:
        path = self._run_dir(run_id) / "run.json"
        if not path.is_file():
            return None
        return QueryRun.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, *, limit: int = 50) -> list[QueryRun]:
        runs: list[QueryRun] = []
        for path in self.root.glob("run_*/run.json"):
            try:
                runs.append(QueryRun.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(runs, key=lambda run: run.created_at, reverse=True)[:limit]

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
        path = self._run_dir(run_id) / "events.jsonl"
        if not path.is_file():
            return []
        events: list[TraceEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = TraceEvent.model_validate_json(line)
            if event.seq > after_seq:
                events.append(event)
        return events

    async def append_event(self, run_id: str, **payload: Any) -> TraceEvent:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self.get(run_id)
            if run is None:
                raise KeyError(run_id)
            event_type = str(payload.pop("event_type"))
            event = TraceEvent(
                seq=run.event_count + 1,
                run_id=run_id,
                type=event_type,
                **payload,
            )
            path = self._run_dir(run_id) / "events.jsonl"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(event.model_dump_json() + "\n")
            run.event_count = event.seq
            self._write_run(run)
        condition = self._conditions.setdefault(run_id, asyncio.Condition())
        async with condition:
            condition.notify_all()
        return event

    async def update(self, run_id: str, **changes: Any) -> QueryRun:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self.get(run_id)
            if run is None:
                raise KeyError(run_id)
            run = run.model_copy(update=changes)
            self._write_run(run)
        condition = self._conditions.setdefault(run_id, asyncio.Condition())
        async with condition:
            condition.notify_all()
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
        """Commit terminal snapshot and terminal event before waking SSE consumers."""
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self.get(run_id)
            if run is None:
                raise KeyError(run_id)
            event = TraceEvent(
                seq=run.event_count + 1,
                run_id=run_id,
                type=event_type,
                stage_id=stage_id,
                label=label,
                status=event_status,
                summary=summary,
                output=output or {},
                trace=trace or {},
                error=error,
            )
            path = self._run_dir(run_id) / "events.jsonl"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(event.model_dump_json() + "\n")
            run = run.model_copy(
                update={
                    "status": status,
                    "completed_at": completed_at,
                    "answer": answer,
                    "error": error,
                    "event_count": event.seq,
                }
            )
            self._write_run(run)
        condition = self._conditions.setdefault(run_id, asyncio.Condition())
        async with condition:
            condition.notify_all()
        return run, event

    async def wait_for_events(
        self, run_id: str, *, after_seq: int, timeout: float = 15.0
    ) -> list[TraceEvent]:
        condition = self._conditions.setdefault(run_id, asyncio.Condition())
        async with condition:
            events = self.list_events(run_id, after_seq=after_seq)
            if events or self._is_terminal(run_id):
                return events
            try:
                await asyncio.wait_for(condition.wait(), timeout=timeout)
            except TimeoutError:
                return []
            return self.list_events(run_id, after_seq=after_seq)

    def recorder(self, run_id: str) -> QueryRunRecorder:
        if self.get(run_id) is None:
            raise KeyError(run_id)
        return QueryRunRecorder(self, run_id)

    def _is_terminal(self, run_id: str) -> bool:
        run = self.get(run_id)
        return run is None or run.status in {"completed", "failed", "cancelled"}

    def _run_dir(self, run_id: str) -> Path:
        target = (self.root / run_id).resolve()
        if target.parent != self.root.resolve():
            raise ValueError("Invalid query run ID")
        return target

    def _write_run(self, run: QueryRun) -> None:
        path = self._run_dir(run.run_id) / "run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)
