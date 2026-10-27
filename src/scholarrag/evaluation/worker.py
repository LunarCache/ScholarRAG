"""Private workspace evaluation execution and conservative metric recovery."""

from __future__ import annotations

import asyncio

from scholarrag.authorization import AuthorizationGuard, bind_authorization
from scholarrag.domain.access import AccessDenied, Principal
from scholarrag.evaluation.jobs import (
    EvaluationJobStore,
    EvaluationMetricJournal,
    EvaluationUncertain,
)
from scholarrag.evaluation.service import RagasEvaluator
from scholarrag.evaluation.workspaces import execution_config
from scholarrag.rag.runs import LeaseLost
from scholarrag.workers import LeasedWorker


class EvaluationWorker(LeasedWorker):
    def __init__(self, runtime):
        super().__init__(
            runtime,
            kind="evaluation",
            poll_seconds=runtime.settings.evaluation_worker_poll_seconds,
            lease_seconds=runtime.settings.evaluation_worker_lease_seconds,
            concurrency=runtime.settings.evaluation_worker_concurrency,
        )

    def identifier(self, item):
        return item.job_id

    def claim(self):
        claimed = self.runtime.evaluation_jobs.claim_next(self.worker_id)
        if claimed is None:
            return None
        job, token = claimed
        return job, EvaluationJobStore(
            self.settings, workspace_id=job.workspace_id, lease_token=token
        )

    def context(self, job):
        return self.runtime.identities.context_for(
            Principal(job.owner_id, job.actor_session_id), job.workspace_id
        )

    def validate(self, job):
        self.runtime.workspace_evaluations.read(self.context(job), job.job_id)

    async def _execute(self, job, store):
        try:
            context = self.context(job)
            self.runtime.workspace_evaluations.read(context, job.job_id)
            parent = self.runtime.workspace_queries.read(context, job.query_run_id)
            guard = AuthorizationGuard(
                self.runtime.identities,
                context,
                tuple(parent.document_ids),
                lease_check=lambda: store.check_execution(job.job_id),
            )
            with (
                bind_authorization(guard),
                self.runtime.telemetry.trace(
                    "scholarrag.evaluation",
                    trace_id=job.job_id.removeprefix("evjob_"),
                    user_id=job.owner_id,
                    session_id=parent.conversation_id,
                    metadata={
                        "workspace_id": job.workspace_id,
                        "job_id": job.job_id,
                        "query_run_id": job.query_run_id,
                    },
                ),
            ):
                if job.execution_config != execution_config(self.settings):
                    raise ValueError("Evaluation configuration changed")
                result = store.result_checkpoint(job.job_id)
                if result is None:
                    saved_metrics = store.metrics(job.job_id)
                    if job.status == "running" and (
                        "__initialized" not in saved_metrics
                        or any(record["state"] != "done" for record in saved_metrics.values())
                    ):
                        raise EvaluationUncertain(
                            "Judge outcome unknown; unfinished metrics were not replayed"
                        )
                    await store.start(job.job_id)
                    evaluator = RagasEvaluator(self.settings)
                    evaluator.checkpoints = EvaluationMetricJournal(store, job.job_id)
                    result = await evaluator.evaluate_answer(
                        case=job.case,
                        answer=parent.answer,
                        parent_query_run_id=parent.run_id,
                        events=store.recorder(job.job_id),
                    )
                    guard.check()
                    if (
                        result.parent_query_run_id,
                        result.answer_trace_id,
                        result.case.question,
                        result.case.document_ids,
                    ) != (
                        parent.run_id,
                        parent.answer.trace_id,
                        job.case.question,
                        job.case.document_ids,
                    ):
                        raise AccessDenied("Evaluation result source mismatch")
                    store.save_result(job.job_id, result)
                guard.check()
                await store.complete(
                    job.job_id,
                    result,
                    authorize=lambda connection: (
                        self.runtime.workspace_evaluations.authorize_publication(
                            context, job, connection
                        )
                    ),
                )
        except asyncio.CancelledError:
            revoked = False
            try:
                AuthorizationGuard(
                    self.runtime.identities, self.context(job), tuple(job.case.document_ids)
                ).check()
            except AccessDenied:
                revoked = True
            await store.cancel(
                job.job_id,
                error="Authorization revoked" if revoked else "Cancelled by user",
            )
        except AccessDenied:
            await store.fail(job.job_id, "Authorization unavailable")
        except EvaluationUncertain:
            await store.fail(
                job.job_id, "Judge outcome unknown; unfinished metrics were not replayed"
            )
        except LeaseLost:
            raise
        except Exception:  # noqa: BLE001 - persist a redacted terminal failure
            # LeaseLost must propagate to the supervisor; a stale executor cannot
            # mutate its row even while trying to persist a failure.
            await store.fail(job.job_id, "Evaluation execution failed")
