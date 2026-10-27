"""Private evaluation APIs, metric checkpoints and real worker process boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_workspace_identity import add_document
from test_workspace_identity import team as team  # noqa: PLC0414

from scholarrag.api.main import create_app
from scholarrag.api.schemas import QueryRequest
from scholarrag.authorization import AuthorizationGuard, bind_authorization
from scholarrag.domain.access import AccessDenied
from scholarrag.domain.models import Answer
from scholarrag.evaluation.jobs import (
    EvaluationJobStore,
    EvaluationMetricJournal,
    EvaluationUncertain,
)
from scholarrag.evaluation.service import EvaluationRun, RagasEvaluator
from scholarrag.evaluation.transport import AuthorizedJudgeTransport
from scholarrag.evaluation.worker import EvaluationWorker
from scholarrag.evaluation.workspaces import WorkspaceEvaluationRequest
from scholarrag.providers.siliconflow import SiliconFlowClient
from scholarrag.rag.runs import LeaseLost, PipelineTracer, QueryRunStore, utc_now
from scholarrag.runtime import Runtime


async def parent(team, runtime, context=None, document_ids=None):
    context = context or team.owner
    run = await runtime.workspace_queries.create(
        context,
        QueryRequest(
            question="synthetic evaluation question", document_ids=document_ids or [], agentic=False
        ),
    )
    claimed, token = runtime.query_runs.claim_next("fixture")
    assert claimed.run_id == run.run_id
    store = QueryRunStore(runtime.settings, workspace_id=run.workspace_id, lease_token=token)
    completed, _ = await store.finalize(
        run.run_id,
        status="completed",
        completed_at=utc_now(),
        answer=Answer(
            answer="Synthetic answer for isolated testing.", citations=[], trace_id=run.trace_id
        ),
        event_type="run.completed",
        stage_id="response.persist",
        label="fixture",
        event_status="success",
        summary="fixture",
    )
    return completed


@pytest.fixture
async def environment(team, monkeypatch):
    team.settings.authorization_poll_seconds = 0.1
    team.settings.evaluation_worker_poll_seconds = 0.02
    team.settings.evaluation_worker_lease_seconds = 1
    runtime = Runtime(team.settings)
    run = await parent(team, runtime)
    calls = []
    original_evaluate = RagasEvaluator.evaluate_answer

    async def score(self, *, case, answer, parent_query_run_id=None, events=None):
        self.checkpoints.initialize()

        async def operation():
            calls.append(case.question)
            await asyncio.sleep(0)
            return SimpleNamespace(value=0.75, reason="synthetic")

        metric = await self._score_metric(
            "faithfulness", operation, tracer=PipelineTracer(events), input={}
        )
        return make_result(case, answer, parent_query_run_id, {"faithfulness": metric})

    monkeypatch.setattr(RagasEvaluator, "evaluate_answer", score)
    return SimpleNamespace(
        runtime=runtime,
        run=run,
        calls=calls,
        app=create_app(team.settings),
        prefix=f"/api/workspaces/{team.workspace}",
        original_evaluate=original_evaluate,
    )


def make_result(case, answer, run_id, metrics):
    return EvaluationRun(
        run_id=f"eval_{uuid.uuid4().hex}",
        created_at=datetime.now(UTC),
        case=case,
        answer=answer.answer,
        answer_trace_id=answer.trace_id,
        retrieved_chunk_ids=[],
        retrieved_contexts=[],
        metrics=metrics,
        artifact_path="/private/synthetic-evaluation.json",
        parent_query_run_id=run_id,
    )


def client(team, env, name="owner"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {team.sessions[name][1]}",
            "Idempotency-Key": "test-evaluation",
        },
    )


async def submit(team, env, *, key="test-evaluation", reference=None):
    return await env.runtime.workspace_evaluations.create(
        team.owner,
        WorkspaceEvaluationRequest(query_run_id=env.run.run_id, reference=reference),
        key,
    )


async def settle(team):
    worker = EvaluationWorker(Runtime(team.settings))
    await worker.tick()
    await asyncio.gather(*worker.active.values())
    return worker


def expire(team, job_id):
    with team.store._transaction() as connection:
        connection.execute(
            "UPDATE evaluation_job_snapshots SET lease_until=0 WHERE job_id=?", (job_id,)
        )


async def test_private_api_complete_sse_and_original_answer_is_unchanged(team, environment):
    env = environment
    before = env.runtime.workspace_queries.read(team.owner, env.run.run_id)
    async with client(team, env) as http:
        response = await http.post(
            f"{env.prefix}/evaluations",
            json={"query_run_id": env.run.run_id, "reference": "User supplied, not verified Gold."},
        )
        assert response.status_code == 202, response.text
        created = response.json()
        assert created["status"] == "queued" and created["case"]["review_status"] == "candidate"
        assert "actor_session_id" not in created and "artifact_dir" not in created
        await settle(team)
        final = (await http.get(f"{env.prefix}/evaluations/{created['job_id']}")).json()
        assert (
            final["status"] == "completed"
            and final["result"]["metrics"]["faithfulness"]["value"] == 0.75
        )
        assert "artifact_path" not in final["result"]
        assert len(env.calls) == 1
        latest = await http.get(f"{env.prefix}/query-runs/{env.run.run_id}/evaluations/latest")
        assert latest.json()["job_id"] == created["job_id"]
        events = (await http.get(f"{env.prefix}/evaluations/{created['job_id']}/event-log")).json()
        assert [item["seq"] for item in events] == list(range(1, final["event_count"] + 1))
        assert events[-1]["type"] == "evaluation.completed"
        stream = await http.get(
            f"{env.prefix}/evaluations/{created['job_id']}/events", headers={"Last-Event-ID": "1"}
        )
        assert (
            stream.status_code == 200
            and "evaluation.completed" in stream.text
            and "evaluation.queued" not in stream.text
        )
        assert (
            await http.get(
                f"{env.prefix}/evaluations/{created['job_id']}/events",
                headers={"Last-Event-ID": "-1"},
            )
        ).status_code == 422
    assert env.runtime.workspace_queries.read(team.owner, env.run.run_id) == before


async def test_foreign_user_admin_and_space_cannot_read_evaluate_cancel_or_stream(
    team, environment
):
    env = environment
    job = await submit(team, env)
    for name in ("member", "admin", "other_owner"):
        async with client(team, env, name) as http:
            response = await http.post(
                f"{env.prefix}/evaluations", json={"query_run_id": env.run.run_id}
            )
            assert response.status_code == 403
            for method, suffix in (
                ("GET", ""),
                ("GET", "/event-log"),
                ("GET", "/events"),
                ("POST", "/cancel"),
            ):
                assert (
                    await http.request(method, f"{env.prefix}/evaluations/{job.job_id}{suffix}")
                ).status_code == 403
    async with client(team, env) as http:
        assert (
            await http.get(f"/api/workspaces/{team.other}/evaluations/{job.job_id}")
        ).status_code == 403
        assert (await http.get(f"/api/evaluations/{job.job_id}")).status_code == 501
    assert env.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {"document_ids": ["foreign"]},
        {"source_chunk_ids": ["foreign"]},
        {"question": "override"},
        {"review_status": "reviewed"},
    ],
)
async def test_api_rejects_client_scope_and_gold_forgery(team, environment, extra):
    async with client(team, environment) as http:
        result = await http.post(
            f"{environment.prefix}/evaluations",
            json={"query_run_id": environment.run.run_id, **extra},
        )
        assert result.status_code == 422
    assert environment.runtime.workspace_evaluations.store(team.workspace).list() == []


async def test_idempotency_survives_completion_and_changed_request_is_rejected(team, environment):
    first = await submit(team, environment)
    await settle(team)
    repeat = await submit(team, environment)
    assert first.job_id == repeat.job_id
    async with client(team, environment) as http:
        conflict = await http.post(
            f"{environment.prefix}/evaluations",
            json={"query_run_id": environment.run.run_id, "reference": "changed"},
        )
        assert conflict.status_code == 409
    await settle(team)
    assert len(environment.calls) == 1


async def test_capacity_and_queued_cancellation_are_shared_and_persistent(team, environment):
    environment.runtime.workspace_evaluations.settings.workspace_evaluation_max_per_user = 1
    first = await submit(team, environment)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await submit(team, environment, key="second")
    assert error.value.status_code == 429
    await environment.runtime.workspace_evaluations.cancel(team.owner, first.job_id)
    await settle(team)
    assert environment.calls == []
    assert (
        environment.runtime.workspace_evaluations.read(team.owner, first.job_id).status
        == "cancelled"
    )


async def test_only_completed_private_answer_can_be_submitted(team, environment):
    queued = await environment.runtime.workspace_queries.create(
        team.owner, QueryRequest(question="unfinished", document_ids=[])
    )
    async with client(team, environment) as http:
        result = await http.post(
            f"{environment.prefix}/evaluations", json={"query_run_id": queued.run_id}
        )
        assert result.status_code == 409
        result = await http.post(
            f"{environment.prefix}/evaluations",
            headers={"Idempotency-Key": " "},
            json={"query_run_id": environment.run.run_id},
        )
        assert result.status_code == 422


async def test_checkpoint_result_is_restored_without_judge_call(team, environment, monkeypatch):
    job = await submit(team, environment)
    worker = EvaluationWorker(environment.runtime)
    _claimed, store = worker.claim()
    result = make_result(job.case, environment.run.answer, job.query_run_id, {})
    await store.start(job.job_id)
    store.save_result(job.job_id, result)
    expire(team, job.job_id)
    await settle(team)
    assert (
        environment.runtime.workspace_evaluations.read(team.owner, job.job_id).status == "completed"
    )
    assert environment.calls == []


async def test_metric_checkpoint_reuse_and_unfinished_call_is_never_replayed(team, environment):
    job = await submit(team, environment)
    _claimed, store = EvaluationWorker(environment.runtime).claim()
    journal = EvaluationMetricJournal(store, job.job_id)
    evaluator = RagasEvaluator(team.settings)
    evaluator.checkpoints = journal
    calls = []

    async def score():
        calls.append(1)
        return SimpleNamespace(value=0.8, reason="synthetic")

    with bind_authorization(
        AuthorizationGuard(
            team.store, team.owner, (), lease_check=lambda: store.check_execution(job.job_id)
        )
    ):
        journal.initialize()
        first = await evaluator._score_metric("metric", score, tracer=PipelineTracer(), input={})
        second = await evaluator._score_metric("metric", score, tracer=PipelineTracer(), input={})
        assert first == second and calls == [1]
        journal.begin("uncertain")
        with pytest.raises(EvaluationUncertain):
            await evaluator._score_metric("uncertain", score, tracer=PipelineTracer(), input={})
    assert calls == [1]


async def test_recovery_between_metrics_reuses_finished_score(team, environment):
    job = await submit(team, environment)
    _, store = EvaluationWorker(environment.runtime).claim()
    await store.start(job.job_id)
    journal = EvaluationMetricJournal(store, job.job_id)
    journal.initialize()
    from scholarrag.evaluation.service import MetricScore

    journal.finish("faithfulness", MetricScore(value=0.66))
    expire(team, job.job_id)
    await settle(team)
    final = environment.runtime.workspace_evaluations.read(team.owner, job.job_id)
    assert final.status == "completed" and final.result.metrics["faithfulness"].value == 0.66
    assert environment.calls == []


async def test_unknown_metric_and_unknown_evaluator_interruption_fail_without_paid_replay(
    team, environment
):
    first = await submit(team, environment)
    _, store = EvaluationWorker(environment.runtime).claim()
    await store.start(first.job_id)
    journal = EvaluationMetricJournal(store, first.job_id)
    journal.initialize()
    journal.begin("faithfulness")
    expire(team, first.job_id)
    await settle(team)
    assert (
        environment.runtime.workspace_evaluations.read(team.owner, first.job_id).status == "failed"
    )
    second = await submit(team, environment, key="new-explicit-attempt")
    _, second_store = EvaluationWorker(environment.runtime).claim()
    await second_store.start(second.job_id)
    expire(team, second.job_id)
    await settle(team)
    assert (
        environment.runtime.workspace_evaluations.read(team.owner, second.job_id).status == "failed"
    )
    assert environment.calls == []


async def test_stale_executor_cannot_write_metric_result_or_terminal_state(team, environment):
    job = await submit(team, environment)
    _, old = EvaluationWorker(environment.runtime).claim()
    await old.start(job.job_id)
    expire(team, job.job_id)
    _, new = EvaluationWorker(environment.runtime).claim()
    with pytest.raises(LeaseLost):
        old.save_metric(job.job_id, "stale", {"state": "done"})
    with pytest.raises(LeaseLost):
        old.save_result(
            job.job_id, make_result(job.case, environment.run.answer, job.query_run_id, {})
        )
    with pytest.raises(LeaseLost):
        await old.fail(job.job_id, "stale")
    new.check_execution(job.job_id)


async def test_revocation_interrupts_inflight_judge_without_publishing(
    team, environment, monkeypatch
):
    job = await submit(team, environment)
    started = asyncio.Event()

    async def blocked(self, **kwargs):
        self.checkpoints.initialize()
        self.checkpoints.begin("blocked")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RagasEvaluator, "evaluate_answer", blocked)
    worker = EvaluationWorker(Runtime(team.settings))
    await worker.tick()
    await asyncio.wait_for(started.wait(), 1)
    team.store.revoke_session(team.owner.session_id)
    await asyncio.wait_for(asyncio.gather(*worker.active.values()), 1)
    final = environment.runtime.workspace_evaluations.store(team.workspace).get(job.job_id)
    assert (
        final.status == "cancelled"
        and final.result is None
        and final.error == "Authorization revoked"
    )


async def test_authenticated_sse_stops_on_revocation_before_next_event(team, environment):
    job = await submit(team, environment)

    async def connected():
        return False

    stream = environment.runtime.workspace_evaluations.events(
        team.owner, job.job_id, after_seq=0, disconnected=connected
    )
    assert "evaluation.queued" in await anext(stream)
    team.store.revoke_session(team.owner.session_id)
    assert "access.revoked" in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_judge_transport_rechecks_authorization_after_capacity_wait(team):
    settings = team.settings.model_copy(update={"provider_max_concurrency": 1})
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={"ok": True})

    transport = AuthorizedJudgeTransport(settings, httpx.MockTransport(handler))
    provider = SiliconFlowClient(settings)
    async with httpx.AsyncClient(transport=transport) as http:
        with bind_authorization(AuthorizationGuard(team.store, team.owner, ())):
            async with provider._request_slot():
                pending = asyncio.create_task(
                    http.post("https://fake-judge.example/v1/chat/completions", json={})
                )
                await asyncio.sleep(0.01)
                team.store.revoke_session(team.owner.session_id)
            with pytest.raises(AccessDenied):
                await pending
    await provider.close()
    assert seen == []


async def test_evaluator_rejects_foreign_retrieval_chunks_before_judge(
    team, environment, monkeypatch
):
    from test_tool_agent import make_chunk

    chunk = make_chunk("foreign-evidence").model_copy(update={"document_id": "not-authorized"})
    evaluator = RagasEvaluator(team.settings)
    monkeypatch.setattr(evaluator.catalog, "chunks_by_ids", lambda ids: {chunk.id: chunk})
    answer = environment.run.answer.model_copy(
        update={"retrieval_debug": {"retrieved_chunk_ids": [chunk.id]}}
    )
    from scholarrag.evaluation.service import EvaluationCase

    with (
        bind_authorization(AuthorizationGuard(team.store, team.owner, ())),
        pytest.raises(AccessDenied),
    ):
        await environment.original_evaluate(
            evaluator, case=EvaluationCase(question="synthetic", document_ids=[]), answer=answer
        )


async def test_final_snapshot_and_event_are_rolled_back_together(team, environment, monkeypatch):
    job = await submit(team, environment)
    _, store = EvaluationWorker(environment.runtime).claim()
    await store.start(job.job_id)
    before = store.get(job.job_id)
    original = store._write

    def broken(connection, value):
        original(connection, value)
        raise OSError("simulated write failure")

    monkeypatch.setattr(store, "_write", broken)
    with pytest.raises(OSError):
        await store.complete(
            job.job_id, make_result(job.case, environment.run.answer, job.query_run_id, {})
        )
    assert store.get(job.job_id) == before
    assert store.list_events(job.job_id)[-1].type == "evaluation.started"


async def test_two_real_evaluation_processes_score_once_and_commit_private_result(
    team, environment, tmp_path
):
    job = await submit(team, environment)
    script = tmp_path / "evaluation_child.py"
    script.write_text("""import asyncio, socket, sys
from pathlib import Path
from types import SimpleNamespace
from scholarrag.config import Settings
from scholarrag.runtime import Runtime
from scholarrag.evaluation.worker import EvaluationWorker
from scholarrag.evaluation.service import RagasEvaluator
from scholarrag.rag.runs import PipelineTracer

def deny(*args, **kwargs):
    raise RuntimeError("Network disabled")
socket.socket.connect = deny
async def scores(self, *, question, response, contexts, reference=None, events=None):
    self.checkpoints.initialize()
    async def operation():
        with (self.settings.data_dir / "synthetic-judge-calls.txt").open("a") as stream:
            stream.write("judge\\n")
        await asyncio.sleep(0.15)
        return SimpleNamespace(value=0.7, reason="synthetic")
    score = await self._score_metric("faithfulness", operation, tracer=PipelineTracer(events), input={})
    return {"faithfulness": score}
RagasEvaluator.evaluate_response = scores
async def main():
    runtime = Runtime(Settings(_env_file=None, data_dir=Path(sys.argv[1]), auth_mode="session"))
    worker = EvaluationWorker(runtime)
    store = runtime.workspace_evaluations.store(sys.argv[2])
    for _ in range(200):
        await worker.tick()
        job = store.get(sys.argv[3])
        if job.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.02)
    await asyncio.gather(*worker.active.values())
    assert job.status == "completed", job.error
asyncio.run(main())
""")

    async def child():
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(script), str(team.settings.data_dir), team.workspace, job.job_id],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "SCHOLARRAG_LANGFUSE_ENABLED": "false"},
        )

    results = await asyncio.gather(child(), child())
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    assert (team.settings.data_dir / "synthetic-judge-calls.txt").read_text().splitlines() == [
        "judge"
    ]
    final = environment.runtime.workspace_evaluations.read(team.owner, job.job_id)
    assert final.status == "completed" and final.result.metrics["faithfulness"].value == 0.7
    assert Path(final.result.artifact_path).is_relative_to(
        team.settings.data_dir / "workspaces" / team.workspace
    )


async def test_running_cancel_is_observed_by_independent_worker(team, environment, monkeypatch):
    job = await submit(team, environment)
    started = asyncio.Event()

    async def blocked(self, **kwargs):
        self.checkpoints.initialize()
        self.checkpoints.begin("blocked")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RagasEvaluator, "evaluate_answer", blocked)
    worker = EvaluationWorker(Runtime(team.settings))
    await worker.tick()
    await asyncio.wait_for(started.wait(), 1)
    await environment.runtime.workspace_evaluations.cancel(team.owner, job.job_id)
    await asyncio.wait_for(asyncio.gather(*worker.active.values()), 1)
    assert (
        environment.runtime.workspace_evaluations.read(team.owner, job.job_id).status == "cancelled"
    )


async def test_document_grant_revocation_hides_results_and_blocks_queued_judge(team, environment):
    add_document(team, "evaluation-paper")
    team.store.assign_document("evaluation-paper", team.workspace, team.owner.user_id)
    run = await parent(team, environment.runtime, document_ids=["evaluation-paper"])
    job = await environment.runtime.workspace_evaluations.create(
        team.owner, WorkspaceEvaluationRequest(query_run_id=run.run_id), "document-bound"
    )
    team.store.unassign_document("evaluation-paper", team.workspace)
    async with client(team, environment) as http:
        assert (await http.get(f"{environment.prefix}/evaluations/{job.job_id}")).status_code == 403
    await settle(team)
    final = environment.runtime.workspace_evaluations.store(team.workspace).get(job.job_id)
    assert final.status == "failed" and final.error == "Authorization unavailable"
    assert environment.calls == []


async def test_atomic_admission_failure_does_not_publish_job_or_audit(
    team, environment, monkeypatch
):
    def fail_event(*args, **kwargs):
        raise OSError("simulated event failure")

    monkeypatch.setattr(EvaluationJobStore, "_event", fail_event)
    with pytest.raises(OSError):
        await submit(team, environment)
    assert environment.runtime.workspace_evaluations.store(team.workspace).list() == []
    with team.store._transaction(write=False) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM auth_audit WHERE action='evaluation.created'"
            ).fetchone()[0]
            == 0
        )


async def test_concurrent_api_instances_share_idempotency(team, environment):
    from concurrent.futures import ThreadPoolExecutor

    runtimes = [Runtime(team.settings), Runtime(team.settings)]

    def create(index):
        return asyncio.run(
            runtimes[index].workspace_evaluations.create(
                team.owner,
                WorkspaceEvaluationRequest(query_run_id=environment.run.run_id),
                "concurrent-request",
            )
        ).job_id

    with ThreadPoolExecutor(2) as pool:
        ids = await asyncio.to_thread(lambda: list(pool.map(create, [0, 1])))
    assert ids[0] == ids[1]
    assert len(environment.runtime.workspace_evaluations.store(team.workspace).list()) == 1


async def test_workspace_metric_errors_are_redacted_in_events_and_result(team, environment):
    job = await submit(team, environment)
    _, store = EvaluationWorker(environment.runtime).claim()
    await store.start(job.job_id)
    evaluator = RagasEvaluator(team.settings)
    evaluator.checkpoints = EvaluationMetricJournal(store, job.job_id)

    async def failure():
        raise RuntimeError("signed-uri?token=do-not-expose")

    with bind_authorization(
        AuthorizationGuard(
            team.store, team.owner, (), lease_check=lambda: store.check_execution(job.job_id)
        )
    ):
        evaluator.checkpoints.initialize()
        result = await evaluator._score_metric(
            "failed-metric", failure, tracer=PipelineTracer(store.recorder(job.job_id)), input={}
        )
    assert result.value is None and result.reason == "Judge metric failed"
    assert "do-not-expose" not in json.dumps(
        [event.model_dump(mode="json") for event in store.list_events(job.job_id)]
    )


async def test_authorization_is_rechecked_in_terminal_publication_transaction(
    team, environment, monkeypatch
):
    job = await submit(team, environment)
    worker = EvaluationWorker(environment.runtime)
    claimed, store = worker.claim()
    original = store.complete

    async def revoke_before_commit(job_id, result, **kwargs):
        team.store.revoke_session(team.owner.session_id)
        return await original(job_id, result, **kwargs)

    monkeypatch.setattr(store, "complete", revoke_before_commit)
    await worker._execute(claimed, store)
    final = environment.runtime.workspace_evaluations.store(team.workspace).get(job.job_id)
    assert (
        final.status == "failed"
        and final.result is None
        and final.error == "Authorization unavailable"
    )
