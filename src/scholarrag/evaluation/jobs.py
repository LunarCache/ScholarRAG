from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from scholarrag.config import Settings
from scholarrag.evaluation.service import EvaluationCase, EvaluationRun
from scholarrag.rag.runs import EventSink, QueryRunRecorder, TraceEvent, utc_now

EvaluationJobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class EvaluationJob(BaseModel):
    schema_version: str = "1.0"
    job_id: str
    status: EvaluationJobStatus = "queued"
    fingerprint: str
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


class EvaluationJobStore:
    def __init__(self, settings: Settings) -> None:
        self.root = settings.data_dir / "evaluation_jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._create_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._conditions: dict[str, asyncio.Condition] = {}

    @staticmethod
    def fingerprint(
        case: EvaluationCase,
        *,
        query_run_id: str | None,
        judge_model: str,
        judge_enable_thinking: bool,
    ) -> str:
        case_payload = case.model_dump(mode="json", exclude={"case_id"})
        payload = {
            "case": case_payload,
            "query_run_id": query_run_id,
            "judge_model": judge_model,
            "judge_enable_thinking": judge_enable_thinking,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def create_or_get_active(
        self,
        *,
        case: EvaluationCase,
        query_run_id: str | None,
        fingerprint: str,
    ) -> tuple[EvaluationJob, bool]:
        async with self._create_lock:
            for job in self.list(limit=10_000):
                if job.fingerprint == fingerprint and job.status in {"queued", "running"}:
                    return job, False
            job_id = f"evjob_{uuid.uuid4().hex}"
            job_dir = self.root / job_id
            job_dir.mkdir(parents=True, exist_ok=False)
            job = EvaluationJob(
                job_id=job_id,
                fingerprint=fingerprint,
                query_run_id=query_run_id,
                case=case,
                artifact_dir=str(job_dir),
            )
            self._write_job(job)
            await self.append_event(
                job_id,
                event_type="evaluation.queued",
                stage_id="evaluation.request",
                label="Ragas 评测已排队",
                status="pending",
                component="scholarrag.evaluation",
                summary="后台评测任务已创建",
                input={"query_run_id": query_run_id},
            )
            return self.get(job_id) or job, True

    def get(self, job_id: str) -> EvaluationJob | None:
        path = self._job_dir(job_id) / "job.json"
        if not path.is_file():
            return None
        return EvaluationJob.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, *, limit: int = 100) -> list[EvaluationJob]:
        jobs: list[EvaluationJob] = []
        for path in self.root.glob("evjob_*/job.json"):
            try:
                jobs.append(EvaluationJob.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)[:limit]

    def latest_for_query_run(self, query_run_id: str) -> EvaluationJob | None:
        return next(
            (job for job in self.list(limit=10_000) if job.query_run_id == query_run_id),
            None,
        )

    def list_events(self, job_id: str, *, after_seq: int = 0) -> list[TraceEvent]:
        path = self._job_dir(job_id) / "events.jsonl"
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

    async def append_event(self, job_id: str, **payload: Any) -> TraceEvent:
        lock = self._locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            event = TraceEvent(
                seq=job.event_count + 1,
                run_id=job_id,
                type=str(payload.pop("event_type")),
                **payload,
            )
            path = self._job_dir(job_id) / "events.jsonl"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(event.model_dump_json() + "\n")
            job.event_count = event.seq
            self._write_job(job)
        await self._notify(job_id)
        return event

    async def start(self, job_id: str) -> EvaluationJob:
        job = await self.update(job_id, status="running", started_at=utc_now(), error=None)
        await self.append_event(
            job_id,
            event_type="evaluation.started",
            stage_id="evaluation.request",
            label="开始 Ragas 评测",
            status="running",
            component="scholarrag.evaluation",
            summary="正在调用 Judge 指标",
        )
        return job

    async def complete(self, job_id: str, result: EvaluationRun) -> EvaluationJob:
        await self.append_event(
            job_id,
            event_type="evaluation.completed",
            stage_id="evaluation.persist",
            label="Ragas 评测完成",
            status="success",
            component="scholarrag.evaluation",
            summary=f"已保存 {len(result.metrics)} 个指标",
            output={"evaluation_run_id": result.run_id, "artifact_path": result.artifact_path},
        )
        return await self.update(
            job_id,
            status="completed",
            completed_at=utc_now(),
            result=result,
            error=None,
        )

    async def fail(self, job_id: str, error: str) -> EvaluationJob:
        await self.append_event(
            job_id,
            event_type="evaluation.failed",
            stage_id="evaluation.persist",
            label="Ragas 评测失败",
            status="failed",
            component="scholarrag.evaluation",
            summary="评测任务失败，可安全重试",
            error=error,
        )
        return await self.update(
            job_id,
            status="failed",
            completed_at=utc_now(),
            error=error,
        )

    async def cancel(self, job_id: str) -> EvaluationJob:
        await self.append_event(
            job_id,
            event_type="evaluation.cancelled",
            stage_id="evaluation.persist",
            label="Ragas 评测已取消",
            status="cancelled",
            component="scholarrag.evaluation",
            summary="已取消评测，问答结果保持不变",
        )
        return await self.update(
            job_id,
            status="cancelled",
            completed_at=utc_now(),
            error="Cancelled by user",
        )

    async def update(self, job_id: str, **changes: Any) -> EvaluationJob:
        lock = self._locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job = job.model_copy(update=changes)
            self._write_job(job)
        await self._notify(job_id)
        return job

    async def wait_for_events(
        self, job_id: str, *, after_seq: int, timeout: float = 15.0
    ) -> list[TraceEvent]:
        condition = self._conditions.setdefault(job_id, asyncio.Condition())
        async with condition:
            events = self.list_events(job_id, after_seq=after_seq)
            if events or self._is_terminal(job_id):
                return events
            try:
                await asyncio.wait_for(condition.wait(), timeout=timeout)
            except TimeoutError:
                return []
            return self.list_events(job_id, after_seq=after_seq)

    async def recover_interrupted(self) -> list[str]:
        recovered: list[str] = []
        for job in self.list(limit=10_000):
            if job.status not in {"queued", "running"}:
                continue
            await self.fail(job.job_id, "Backend restarted before this evaluation completed")
            recovered.append(job.job_id)
        return recovered

    def recorder(
        self, job_id: str, *, parent: QueryRunRecorder | None = None
    ) -> EvaluationJobRecorder:
        if self.get(job_id) is None:
            raise KeyError(job_id)
        return EvaluationJobRecorder(self, job_id, parent)

    async def _notify(self, job_id: str) -> None:
        condition = self._conditions.setdefault(job_id, asyncio.Condition())
        async with condition:
            condition.notify_all()

    def _is_terminal(self, job_id: str) -> bool:
        job = self.get(job_id)
        return job is None or job.status in {"completed", "failed", "cancelled"}

    def _job_dir(self, job_id: str) -> Path:
        target = (self.root / job_id).resolve()
        if target.parent != self.root.resolve():
            raise ValueError("Invalid evaluation job ID")
        return target

    def _write_job(self, job: EvaluationJob) -> None:
        path = self._job_dir(job.job_id) / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(job.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)
