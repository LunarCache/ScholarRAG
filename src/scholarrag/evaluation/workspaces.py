"""Private evaluation admission: only the caller's completed workspace answer."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from scholarrag.authorization import AuthorizationGuard
from scholarrag.config import Settings
from scholarrag.domain.access import AccessContext, AccessDenied, IdentityConflict
from scholarrag.evaluation.jobs import EvaluationJob, EvaluationJobStore
from scholarrag.evaluation.service import EvaluationCase
from scholarrag.rag.runs import QueryRun
from scholarrag.rag.workspaces import WorkspaceQueries
from scholarrag.storage.identity import IdentityStore


class WorkspaceEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query_run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    reference: str | None = Field(default=None, max_length=50000)


def execution_config(settings: Settings) -> dict:
    return {
        key: getattr(settings, key)
        for key in (
            "ragas_judge_model",
            "ragas_judge_enable_thinking",
            "ragas_judge_temperature",
            "ragas_judge_context_window_tokens",
            "unified_embedding_model",
            "siliconflow_base_url",
        )
    }


def public_evaluation(job: EvaluationJob) -> dict:
    result = job.model_dump(
        mode="json", exclude={"actor_session_id", "artifact_dir", "fingerprint", "execution_config"}
    )
    if result["result"]:
        result["result"].pop("artifact_path", None)
    return result


class WorkspaceEvaluations:
    def __init__(self, settings: Settings, identities: IdentityStore, queries: WorkspaceQueries):
        self.settings = settings
        self.identities = identities
        self.queries = queries
        self.stores: dict[str, EvaluationJobStore] = {}

    def store(self, workspace_id):
        if workspace_id not in self.stores:
            self.stores[workspace_id] = EvaluationJobStore(self.settings, workspace_id=workspace_id)
        return self.stores[workspace_id]

    def read(self, context: AccessContext, job_id: str) -> EvaluationJob:
        self.identities.authorize(context, "read")
        job = self.store(context.workspace_id).get(job_id)
        if (
            job is None
            or job.workspace_id != context.workspace_id
            or job.owner_id != context.user_id
        ):
            raise AccessDenied("Evaluation unavailable")
        parent = self.queries.read(context, job.query_run_id)
        if (
            parent.status != "completed"
            or parent.answer is None
            or (job.case.question, job.case.document_ids) != (parent.question, parent.document_ids)
        ):
            raise AccessDenied("Evaluation source unavailable")
        AuthorizationGuard(self.identities, context, tuple(parent.document_ids)).check()
        return job

    def authorize_publication(self, context: AccessContext, job: EvaluationJob, connection):
        self.identities._authorize(connection, context, "query")
        row = connection.execute(
            """SELECT q.payload FROM query_run_snapshots q JOIN auth_query_runs a ON a.run_id=q.run_id
            WHERE q.run_id=? AND q.namespace=? AND a.owner_id=?""",
            (job.query_run_id, context.workspace_id, context.user_id),
        ).fetchone()
        if row is None:
            raise AccessDenied("Evaluation source unavailable")
        parent = QueryRun.model_validate_json(row[0])
        if (
            parent.status != "completed"
            or parent.answer is None
            or (parent.question, parent.document_ids) != (job.case.question, job.case.document_ids)
        ):
            raise AccessDenied("Evaluation source changed")
        if not set(job.case.document_ids).issubset(
            IdentityStore.active_document_ids(connection, context)
        ):
            raise AccessDenied("Evaluation documents unavailable")

    async def create(self, context, request: WorkspaceEvaluationRequest, key: str):
        if not 1 <= len(key.strip()) <= 128:
            raise ValueError("Idempotency-Key must contain 1-128 characters")
        parent = self.queries.read(context, request.query_run_id)
        if parent.status != "completed" or parent.answer is None:
            raise IdentityConflict("Only a completed answer can be evaluated")
        case = EvaluationCase(
            question=parent.question,
            document_ids=parent.document_ids,
            reference=request.reference,
            review_status="candidate",
            generator={
                "source": "workspace_user_reference" if request.reference else "workspace_answer"
            },
        )
        configuration = execution_config(self.settings)
        fingerprint = EvaluationJobStore.fingerprint(
            case,
            query_run_id=parent.run_id,
            judge_model=self.settings.ragas_judge_model,
            judge_enable_thinking=self.settings.ragas_judge_enable_thinking,
            execution_config=configuration,
        )

        def authorize(connection):
            self.identities._authorize(connection, context, "query")
            row = connection.execute(
                """SELECT q.payload FROM query_run_snapshots q JOIN auth_query_runs a ON a.run_id=q.run_id
                WHERE q.run_id=? AND q.namespace=? AND a.owner_id=?""",
                (parent.run_id, context.workspace_id, context.user_id),
            ).fetchone()
            if row is None or QueryRun.model_validate_json(row[0]) != parent:
                raise AccessDenied("Evaluation source changed")
            allowed = set(IdentityStore.active_document_ids(connection, context))
            if not set(parent.document_ids).issubset(allowed):
                raise AccessDenied("Evaluation documents unavailable")

        job, _ = await self.store(context.workspace_id).create_or_get_active(
            case=case,
            query_run_id=parent.run_id,
            fingerprint=fingerprint,
            owner_id=context.user_id,
            actor_session_id=context.session_id,
            execution_config=configuration,
            idempotency_hash=hashlib.sha256(key.encode()).hexdigest(),
            authorize=authorize,
            max_active=self.settings.workspace_evaluation_max_active,
            max_per_user=self.settings.workspace_evaluation_max_per_user,
        )
        return job

    def latest(self, context, run_id):
        self.queries.read(context, run_id)
        job = self.store(context.workspace_id).latest_for_query_run(
            run_id, owner_id=context.user_id
        )
        if job is None:
            raise HTTPException(status_code=404, detail="No evaluation found")
        return self.read(context, job.job_id)

    async def cancel(self, context, job_id):
        self.read(context, job_id)
        await self.store(context.workspace_id).request_cancel(job_id)
        return self.read(context, job_id)

    async def events(
        self, context, job_id, *, after_seq: int, disconnected: Callable[[], Awaitable[bool]]
    ) -> AsyncIterator[str]:
        store = self.store(context.workspace_id)
        cursor = after_seq
        while not await disconnected():
            try:
                job = self.read(context, job_id)
                events = store.list_events(job_id, after_seq=cursor)
                if not events:
                    if job.status in {"completed", "failed", "cancelled"}:
                        return
                    await store.wait_for_events(
                        job_id, after_seq=cursor, timeout=self.settings.authorization_poll_seconds
                    )
                    self.read(context, job_id)
                    yield ": heartbeat\n\n"
                    continue
                for event in events:
                    self.read(context, job_id)
                    cursor = event.seq
                    yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"
            except AccessDenied:
                yield 'event: access.revoked\ndata: {"type":"access.revoked"}\n\n'
                return
