"""Private workspace query ownership and admission; execution remains single-process."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import HTTPException

from scholarrag.api.schemas import QueryRequest
from scholarrag.authorization import AuthorizationGuard
from scholarrag.config import Settings
from scholarrag.domain.access import AccessContext, AccessDenied, IdentityConflict
from scholarrag.rag.runs import QueryRun, QueryRunStore
from scholarrag.rag.scope import DocumentScope
from scholarrag.storage.identity import IdentityStore


def public_run(run: QueryRun) -> dict:
    return run.model_dump(mode="json", exclude={"actor_session_id", "artifact_dir"})


class WorkspaceQueries:
    def __init__(
        self,
        settings: Settings,
        identities: IdentityStore,
        execute: Callable[[str, QueryRunStore], Awaitable[None]],
        tasks: set[asyncio.Task],
    ) -> None:
        self.settings = settings
        self.identities = identities
        self.execute = execute
        self.tasks = tasks
        self.stores: dict[str, QueryRunStore] = {}
        self.active: dict[str, tuple[AccessContext, asyncio.Task]] = {}
        self.lock = asyncio.Lock()

    def store(self, workspace_id: str) -> QueryRunStore:
        if workspace_id not in self.stores:
            self.stores[workspace_id] = QueryRunStore(self.settings, workspace_id=workspace_id)
        return self.stores[workspace_id]

    def read(self, context: AccessContext, run_id: str) -> QueryRun:
        record = self.identities.query_record(context, run_id)
        AuthorizationGuard(self.identities, context, tuple(record["document_ids"])).check()
        run = self.store(context.workspace_id).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Query artifact unavailable")
        if (
            run.workspace_id,
            run.owner_id,
            run.conversation_id,
            run.actor_session_id,
            run.document_ids,
        ) != (
            record["workspace_id"],
            record["owner_id"],
            record["conversation_id"],
            record["actor_session_id"],
            record["document_ids"],
        ):
            raise AccessDenied("Query ownership mismatch")
        return run

    def list(
        self, context: AccessContext, *, limit: int, conversation_id: str | None = None
    ) -> list[dict]:
        runs = []
        for run_id in self.identities.owned_query_ids(
            context, limit=limit, conversation_id=conversation_id
        ):
            try:
                runs.append(public_run(self.read(context, run_id)))
            except AccessDenied:
                # A document grant may have been removed since this historical run.
                continue
        self.identities.authorize(context, "read")
        return runs

    async def create(self, context: AccessContext, request: QueryRequest) -> QueryRun:
        async with self.lock:
            self.identities.authorize(context, "query")
            if (
                len(self.active) >= self.settings.workspace_query_max_active
                or sum(ctx.user_id == context.user_id for ctx, _ in self.active.values())
                >= self.settings.workspace_query_max_per_user
            ):
                raise HTTPException(status_code=429, detail="Too many active query runs")
            try:
                document_ids = DocumentScope.intersect(
                    self.identities.document_ids(context)
                ).resolve(request.document_ids)
            except PermissionError as exc:
                raise AccessDenied("Query documents unavailable") from exc
            conversation_id = request.conversation_id
            if conversation_id is not None:
                self.identities.require_conversation(context, conversation_id)
            if request.parent_run_id:
                parent = self.read(context, request.parent_run_id)
                if conversation_id is None or parent.conversation_id != conversation_id:
                    raise AccessDenied("Parent must belong to the same private conversation")
                if parent.status != "completed" or parent.answer is None:
                    raise IdentityConflict("Parent run is not complete")
                if parent.document_ids != document_ids:
                    raise IdentityConflict("Start a new branch when changing the document scope")
            conversation_id = conversation_id or self.identities.create_conversation(context)
            store = self.store(context.workspace_id)
            run = await store.create(
                question=request.question,
                document_ids=document_ids,
                top_k=request.top_k,
                agentic=request.agentic,
                tool_mode=request.tool_mode,
                conversation_id=conversation_id,
                parent_run_id=request.parent_run_id,
                workspace_id=context.workspace_id,
                owner_id=context.user_id,
                actor_session_id=context.session_id,
            )
            try:
                self.identities.register_query_run(context, run)
            except BaseException:
                shutil.rmtree(store.root / run.run_id)
                raise
            task = asyncio.create_task(self.execute(run.run_id, store))
            self.active[run.run_id] = (context, task)
            self.tasks.add(task)

            def discard(completed: asyncio.Task) -> None:
                self.active.pop(run.run_id, None)
                self.tasks.discard(completed)

            task.add_done_callback(discard)
            return run

    async def cancel(self, context: AccessContext, run_id: str) -> QueryRun:
        self.read(context, run_id)
        active = self.active.get(run_id)
        if active and not active[1].done():
            active[1].cancel()
            await asyncio.gather(active[1], return_exceptions=True)
        current = self.read(context, run_id)
        if current.status in {"queued", "running"}:
            from scholarrag.rag.runs import utc_now

            await self.store(context.workspace_id).finalize(
                run_id,
                status="cancelled",
                completed_at=utc_now(),
                error="Cancelled by user",
                event_type="run.cancelled",
                stage_id="response.persist",
                label="回答已取消",
                event_status="cancelled",
                summary="用户取消了本次问答",
            )
        return self.read(context, run_id)

    async def recover(self) -> None:
        for workspace_id in self.identities.query_workspaces():
            await self.store(workspace_id).recover_interrupted()

    async def events(
        self,
        context: AccessContext,
        run_id: str,
        *,
        after_seq: int,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[str]:
        cursor = after_seq
        store = self.store(context.workspace_id)
        while not await disconnected():
            try:
                current = self.read(context, run_id)
                events = store.list_events(run_id, after_seq=cursor)
                if not events:
                    if current.status in {"completed", "failed", "cancelled"}:
                        return
                    await store.wait_for_events(
                        run_id, after_seq=cursor, timeout=self.settings.authorization_poll_seconds
                    )
                    self.read(context, run_id)
                    yield ": heartbeat\n\n"
                    continue
                for event in events:
                    self.read(context, run_id)
                    cursor = event.seq
                    yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"
            except AccessDenied:
                yield 'event: access.revoked\ndata: {"type":"access.revoked"}\n\n'
                return
