"""Durable workspace query worker. No HTTP server and no automatic paid replay."""

from __future__ import annotations

from scholarrag.authorization import AuthorizationGuard, bind_authorization
from scholarrag.domain.access import AccessDenied, Principal
from scholarrag.rag.execution import _run_query
from scholarrag.rag.runs import QueryCancellation, QueryRun, QueryRunStore, utc_now
from scholarrag.runtime import Runtime
from scholarrag.workers import LeasedWorker


class QueryWorker(LeasedWorker):
    def __init__(self, runtime: Runtime):
        super().__init__(
            runtime,
            kind="query",
            poll_seconds=runtime.settings.query_worker_poll_seconds,
            lease_seconds=runtime.settings.query_worker_lease_seconds,
            concurrency=runtime.settings.query_worker_concurrency,
        )

    def identifier(self, item):
        return item.run_id

    def claim(self):
        claimed = self.runtime.query_runs.claim_next(self.worker_id)
        if claimed is None:
            return None
        run, token = claimed
        return run, QueryRunStore(self.settings, workspace_id=run.workspace_id, lease_token=token)

    async def _execute(self, run: QueryRun, store: QueryRunStore) -> None:
        # Both restored checkpoints and fresh execution revalidate current authority.
        try:
            context = self.runtime.identities.context_for(
                Principal(run.owner_id, run.actor_session_id),
                run.workspace_id,
            )
            self.runtime.workspace_queries.read(context, run.run_id)
            guard = AuthorizationGuard(
                self.runtime.identities,
                context,
                tuple(run.document_ids),
                lease_check=lambda: store.check_execution(run.run_id),
            )
            with bind_authorization(guard):
                answer = store.checkpoint(run.run_id)
                if answer is not None:
                    guard.check()
                    await store.finalize(
                        run.run_id,
                        status="completed",
                        completed_at=utc_now(),
                        answer=answer,
                        event_type="run.completed",
                        stage_id="response.persist",
                        label="恢复已生成的回答",
                        event_status="success",
                        summary="从答案检查点恢复，未再次调用模型",
                        output={"restored_checkpoint": True},
                    )
                elif run.status == "running":
                    await store.finalize(
                        run.run_id,
                        status="failed",
                        completed_at=utc_now(),
                        error="Execution interrupted; provider outcome may be unknown",
                        event_type="run.failed",
                        stage_id="response.persist",
                        label="执行中断",
                        event_status="failed",
                        summary="租约过期且无答案检查点，保留事件并停止，未自动重放供应商请求",
                    )
                else:
                    await _run_query(self.runtime, run.run_id, store=store)
        except QueryCancellation:
            await store.finalize(
                run.run_id,
                status="cancelled",
                completed_at=utc_now(),
                error="Cancelled by user",
                event_type="run.cancelled",
                stage_id="response.persist",
                label="回答已取消",
                event_status="cancelled",
                summary="用户取消了本次问答",
            )
        except AccessDenied:
            await store.finalize(
                run.run_id,
                status="failed",
                completed_at=utc_now(),
                error="Authorization unavailable",
                event_type="run.failed",
                stage_id="response.persist",
                label="授权已失效",
                event_status="failed",
                summary="任务未执行：授权不可用",
            )
