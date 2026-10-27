"""Query execution shared by the local API and durable workspace worker."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from scholarrag.authorization import (
    AuthorizationGuard,
    bind_authorization,
    check_active_authorization,
)
from scholarrag.domain.access import AccessDenied, Principal
from scholarrag.rag.agent import AgenticRAGService
from scholarrag.rag.runs import LeaseLost, QueryCancellation, QueryRunStore, utc_now
from scholarrag.rag.service import RAGService

if TYPE_CHECKING:
    from scholarrag.runtime import Runtime


async def _run_query(runtime: Runtime, run_id: str, *, store: QueryRunStore | None = None) -> None:
    store = store or runtime.query_runs
    try:
        await _execute_query(runtime, run_id, store=store)
    except AccessDenied:
        current = store.get(run_id)
        if current and current.status in {"queued", "running"}:
            await store.finalize(
                run_id,
                status="failed",
                completed_at=utc_now(),
                error="Authorization unavailable",
                event_type="run.failed",
                stage_id="response.persist",
                label="授权已失效",
                event_status="failed",
                summary="任务未执行：授权不可用",
            )


async def _execute_query(runtime: Runtime, run_id: str, *, store: QueryRunStore) -> None:
    store = store or runtime.query_runs
    run = store.get(run_id)
    if run is None:
        return
    guard = None
    try:
        if run.workspace_id is not None:
            context = runtime.identities.context_for(
                Principal(run.owner_id, run.actor_session_id), run.workspace_id
            )
            runtime.workspace_queries.read(context, run_id)
            guard = AuthorizationGuard(
                runtime.identities,
                context,
                tuple(run.document_ids),
                lease_check=lambda: store.check_execution(run_id),
            )
        elif runtime.settings.auth_mode == "session":
            raise AccessDenied("Workspace ownership required")
    except AccessDenied:
        await store.finalize(
            run_id,
            status="failed",
            completed_at=utc_now(),
            error="Authorization unavailable",
            event_type="run.failed",
            stage_id="response.persist",
            label="授权已失效",
            event_status="failed",
            summary="任务未执行：授权不可用",
        )
        return
    with (
        bind_authorization(guard),
        runtime.telemetry.trace(
            "scholarrag.query",
            trace_id=run.trace_id,
            session_id=run.conversation_id,
            user_id=run.owner_id,
            metadata={
                "run_id": run_id,
                "agentic": run.agentic,
                "tool_mode": run.tool_mode,
                "workspace_id": run.workspace_id,
            },
            input={"question": run.question},
        ) as observation,
    ):
        parent_task = asyncio.current_task()

        async def watch_authorization():
            while True:
                await asyncio.sleep(runtime.settings.authorization_poll_seconds)
                try:
                    guard.check()
                except (AccessDenied, LeaseLost, QueryCancellation):
                    parent_task.cancel()
                    return

        watcher = asyncio.create_task(watch_authorization()) if guard is not None else None
        try:
            await _run_query_inner(runtime, run_id, store)
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        final = store.get(run_id)
        if final:
            observation.update(
                output=final.answer.answer if final.answer else None,
                level="ERROR"
                if final.status == "failed"
                else ("WARNING" if final.status == "cancelled" else "DEFAULT"),
                status_message=final.status,
            )


async def _run_query_inner(runtime: Runtime, run_id: str, store: QueryRunStore) -> None:
    run = store.get(run_id)
    if run is None:
        return
    recorder = store.recorder(run_id)
    await store.update(run_id, status="running", started_at=utc_now())
    await recorder.emit(
        event_type="run.started",
        stage_id="request",
        label="接收问题",
        status="success",
        summary="开始执行问答链路",
    )
    try:
        check_active_authorization()
        contextual_question = run.question
        if run.parent_run_id:
            parent = (
                runtime.workspace_queries.read(
                    runtime.identities.context_for(
                        Principal(run.owner_id, run.actor_session_id), run.workspace_id
                    ),
                    run.parent_run_id,
                )
                if run.workspace_id is not None
                else store.get(run.parent_run_id)
            )
            if parent and parent.answer:
                contextual_question = (
                    f"上轮问题：{parent.question}\n上轮回答：{parent.answer.answer[:4000]}\n\n"
                    f"当前追问：{run.question}"
                )
                await recorder.emit(
                    event_type="conversation.context",
                    stage_id="request.context",
                    label="加载会话上下文",
                    status="success",
                    component="query_run_store",
                    summary="已加载父问题与回答，形成可审计分支",
                    input={"parent_run_id": parent.run_id},
                )
        service = (
            AgenticRAGService(runtime.settings, tool_calling=run.tool_mode)
            if run.agentic
            else RAGService(runtime.settings)
        )
        answer = await service.answer(
            contextual_question,
            document_ids=run.document_ids,
            top_k=run.top_k,
            events=recorder,
            trace_id=run.trace_id,
        )
        check_active_authorization()
        if run.workspace_id is not None and any(
            citation.document_id not in run.document_ids for citation in answer.citations
        ):
            raise AccessDenied("Answer contains out-of-scope citations")
        await store.save_answer_checkpoint(run_id, answer)
        await store.finalize(
            run_id,
            status="completed",
            completed_at=utc_now(),
            answer=answer,
            event_type="run.completed",
            stage_id="response.persist",
            label="回答完成与 Trace 持久化",
            event_status="success",
            summary=f"回答完成，返回 {len(answer.citations)} 个引用",
            output={
                "answer_chars": len(answer.answer),
                "citation_count": len(answer.citations),
            },
            trace={"trace_id": answer.trace_id},
        )
    except asyncio.CancelledError:
        revoked = False
        try:
            check_active_authorization()
        except AccessDenied:
            revoked = True
        await store.finalize(
            run_id,
            status="cancelled",
            completed_at=utc_now(),
            error="Authorization revoked" if revoked else "Cancelled by user",
            event_type="run.cancelled",
            stage_id="response.persist",
            label="回答已取消",
            event_status="cancelled",
            summary="授权失效，任务停止" if revoked else "本次问答已取消",
        )
    except Exception as exc:  # noqa: BLE001 - run artifact must record terminal failures
        error = (
            "Authorization unavailable"
            if isinstance(exc, AccessDenied)
            else ("Query execution failed" if run.workspace_id else f"{type(exc).__name__}: {exc}")
        )
        await store.finalize(
            run_id,
            status="failed",
            completed_at=utc_now(),
            error=error,
            event_type="run.failed",
            stage_id="response.persist",
            label="问答失败",
            event_status="failed",
            summary="问答链路执行失败",
        )
