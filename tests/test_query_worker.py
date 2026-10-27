"""Durability, lease fencing and recovery tests with no real provider calls."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from test_workspace_identity import team as team  # noqa: PLC0414

from scholarrag.api.schemas import QueryRequest
from scholarrag.domain.models import Answer
from scholarrag.rag.runs import LeaseLost, QueryRunStore
from scholarrag.rag.service import RAGService
from scholarrag.runtime import Runtime
from scholarrag.worker import QueryWorker


async def queued(team, question="durable"):
    runtime = Runtime(team.settings.model_copy(update={"query_worker_lease_seconds": 1}))
    run = await runtime.workspace_queries.create(
        team.owner,
        QueryRequest(question=question, document_ids=[], agentic=False),
    )
    return runtime, run


def leased(runtime, worker_id="first"):
    run, token = runtime.query_runs.claim_next(worker_id)
    return run, QueryRunStore(runtime.settings, workspace_id=run.workspace_id, lease_token=token)


def expire(store, run_id):
    with store.transaction() as connection:
        connection.execute("UPDATE query_run_snapshots SET lease_until=0 WHERE run_id=?", (run_id,))


async def settle(worker):
    await worker.tick()
    await asyncio.gather(*worker.active.values())
    await worker.tick()


async def test_two_independent_claimers_have_only_one_winner(team):
    runtime, run = await queued(team)

    def claim(name):
        return QueryRunStore(runtime.settings).claim_next(name)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, ["first", "second"]))
    assert len([result for result in results if result is not None]) == 1
    assert next(result for result in results if result)[0].run_id == run.run_id


async def test_queue_survives_new_runtime_and_runs_once(team, monkeypatch):
    runtime, run = await queued(team)
    calls = []

    async def answer(self, question, **kwargs):
        calls.append(question)
        return Answer(answer="persisted", citations=[], trace_id=kwargs["trace_id"])

    monkeypatch.setattr(RAGService, "answer", answer)
    worker = QueryWorker(Runtime(runtime.settings))
    await settle(worker)
    await settle(QueryWorker(Runtime(runtime.settings)))
    final = runtime.workspace_queries.read(team.owner, run.run_id)
    assert final.status == "completed" and final.answer.answer == "persisted"
    assert calls == ["durable"]
    events = runtime.workspace_queries.store(team.workspace).list_events(run.run_id)
    assert [event.seq for event in events] == list(range(1, final.event_count + 1))
    assert [event.type for event in events].count("run.completed") == 1


async def test_heartbeat_keeps_lease_and_expired_executor_is_fenced(team):
    runtime, run = await queued(team)
    _, first = leased(runtime)
    assert first.heartbeat(run.run_id) is False
    assert runtime.query_runs.claim_next("second") is None
    await first.update(run.run_id, status="running")
    expire(first, run.run_id)
    claimed, second = leased(runtime, "second")
    assert claimed.status == "running"
    with pytest.raises(LeaseLost):
        await first.update(run.run_id, status="completed")
    with pytest.raises(LeaseLost):
        await first.save_answer_checkpoint(
            run.run_id, Answer(answer="stale", citations=[], trace_id=run.trace_id)
        )
    with pytest.raises(LeaseLost):
        await first.append_event(
            run.run_id, event_type="stale", stage_id="test", label="stale", status="success"
        )
    second.check_execution(run.run_id)


async def test_completed_answer_checkpoint_recovers_without_model_call(team, monkeypatch):
    runtime, run = await queued(team)
    _, store = leased(runtime)
    await store.update(run.run_id, status="running")
    await store.save_answer_checkpoint(
        run.run_id, Answer(answer="saved before crash", citations=[], trace_id=run.trace_id)
    )
    expire(store, run.run_id)

    async def forbidden(*args, **kwargs):
        pytest.fail("Checkpoint recovery must not issue another model request")

    monkeypatch.setattr(RAGService, "answer", forbidden)
    await settle(QueryWorker(Runtime(runtime.settings)))
    final = runtime.workspace_queries.read(team.owner, run.run_id)
    assert final.status == "completed" and final.answer.answer == "saved before crash"
    assert store.list_events(run.run_id)[-1].output == {"restored_checkpoint": True}


async def test_expired_running_without_checkpoint_is_not_replayed(team, monkeypatch):
    runtime, run = await queued(team)
    _, store = leased(runtime)
    await store.update(run.run_id, status="running")
    expire(store, run.run_id)

    async def forbidden(*args, **kwargs):
        pytest.fail("Unknown external effects cannot be retried")

    monkeypatch.setattr(RAGService, "answer", forbidden)
    await settle(QueryWorker(Runtime(runtime.settings)))
    final = runtime.workspace_queries.read(team.owner, run.run_id)
    assert final.status == "failed" and "unknown" in final.error
    assert store.list_events(run.run_id)[-1].type == "run.failed"


async def test_checkpoint_cannot_bypass_revoked_session(team):
    runtime, run = await queued(team)
    _, store = leased(runtime)
    await store.update(run.run_id, status="running")
    await store.save_answer_checkpoint(
        run.run_id, Answer(answer="private checkpoint", citations=[], trace_id=run.trace_id)
    )
    expire(store, run.run_id)
    team.store.revoke_session(team.owner.session_id)
    await settle(QueryWorker(Runtime(runtime.settings)))
    final = store.get(run.run_id)
    assert final.status == "failed" and final.answer is None
    assert final.error == "Authorization unavailable"


async def test_queued_cancel_fences_worker_that_already_claimed(team):
    runtime, run = await queued(team)
    _, store = leased(runtime)
    cancelled = await runtime.workspace_queries.cancel(team.owner, run.run_id)
    assert cancelled.status == "cancelled"
    with pytest.raises(LeaseLost):
        await store.update(run.run_id, status="running")
    assert runtime.query_runs.claim_next("next") is None
    events = store.list_events(run.run_id)
    assert events[-1].type == "run.cancelled"


async def test_persistent_cancel_interrupts_an_independent_executor(team, monkeypatch):
    runtime, run = await queued(team)
    runtime.settings = runtime.settings.model_copy(update={"authorization_poll_seconds": 0.1})
    worker = QueryWorker(Runtime(runtime.settings))
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RAGService, "answer", blocked)
    await worker.tick()
    await asyncio.wait_for(started.wait(), 1)
    result = await runtime.workspace_queries.cancel(team.owner, run.run_id)
    assert result.status in {"running", "cancelled"}
    await asyncio.wait_for(asyncio.gather(*worker.active.values()), 1)
    final = runtime.workspace_queries.read(team.owner, run.run_id)
    assert final.status == "cancelled" and final.answer is None


async def test_cancelled_running_checkpoint_is_never_published(team):
    runtime, run = await queued(team)
    _, store = leased(runtime)
    await store.update(run.run_id, status="running")
    await store.save_answer_checkpoint(
        run.run_id, Answer(answer="not visible", citations=[], trace_id=run.trace_id)
    )
    await runtime.workspace_queries.cancel(team.owner, run.run_id)
    expire(store, run.run_id)
    await settle(QueryWorker(Runtime(runtime.settings)))
    final = store.get(run.run_id)
    assert final.status == "cancelled" and final.answer is None


async def test_failed_terminal_event_rolls_back_snapshot_together(team, monkeypatch):
    runtime, run = await queued(team)
    _, store = leased(runtime)
    await store.update(run.run_id, status="running")
    original = store._write

    def broken(connection, value):
        original(connection, value)
        raise OSError("simulated disk failure")

    monkeypatch.setattr(store, "_write", broken)
    before = store.get(run.run_id)
    from scholarrag.rag.runs import utc_now

    with pytest.raises(OSError):
        await store.finalize(
            run.run_id,
            status="completed",
            completed_at=utc_now(),
            event_type="run.completed",
            stage_id="response.persist",
            label="done",
            event_status="success",
            summary="done",
            answer=Answer(answer="result", citations=[], trace_id=run.trace_id),
        )
    assert store.get(run.run_id) == before
    assert store.list_events(run.run_id)[-1].type == "run.created"


async def test_admission_limits_are_shared_between_api_instances(team):
    runtime, run = await queued(team)
    other = Runtime(runtime.settings.model_copy(update={"workspace_query_max_per_user": 1}))
    with pytest.raises(HTTPException) as error:
        await other.workspace_queries.create(team.owner, QueryRequest(question="over limit"))
    assert error.value.status_code == 429
    assert len(runtime.workspace_queries.store(team.workspace).list()) == 1
    assert team.store.owned_query_ids(team.owner) == [run.run_id]


async def test_worker_shutdown_leaves_recoverable_lease_instead_of_user_cancel(team, monkeypatch):
    runtime, run = await queued(team)
    worker = QueryWorker(runtime)
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RAGService, "answer", blocked)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    final = runtime.workspace_queries.read(team.owner, run.run_id)
    assert final.status == "running"
    expire(runtime.workspace_queries.store(team.workspace), run.run_id)
    await settle(QueryWorker(Runtime(runtime.settings)))
    assert runtime.workspace_queries.read(team.owner, run.run_id).status == "failed"


async def test_real_subprocess_worker_reads_api_queue_and_commits_events(team, tmp_path):
    runtime, run = await queued(team)
    # Exercise a real interpreter boundary with a fake model, isolated data and
    # socket access blocked inside the child as well as in the parent pytest.
    script = tmp_path / "worker_smoke.py"
    script.write_text("""import asyncio, socket, sys
from pathlib import Path
from scholarrag.config import Settings
from scholarrag.runtime import Runtime
from scholarrag.worker import QueryWorker
from scholarrag.rag.service import RAGService
from scholarrag.domain.models import Answer

def deny(*args, **kwargs):
    raise RuntimeError("Network disabled")
socket.socket.connect = deny
async def answer(self, question, **kwargs):
    with (self.settings.data_dir / "fake-model-calls.txt").open("a") as stream:
        stream.write("called\\n")
    await asyncio.sleep(0.2)
    return Answer(answer="child process answer", citations=[], trace_id=kwargs["trace_id"])
RAGService.answer = answer
async def main():
    settings = Settings(_env_file=None, data_dir=Path(sys.argv[1]), auth_mode="session")
    runtime = Runtime(settings)
    worker = QueryWorker(runtime)
    for _ in range(100):
        await worker.tick()
        if runtime.workspace_queries.store(sys.argv[2]).get(sys.argv[3]).status == "completed":
            break
        await asyncio.sleep(0.02)
    await asyncio.gather(*worker.active.values())
    assert runtime.workspace_queries.store(sys.argv[2]).get(sys.argv[3]).status == "completed"
asyncio.run(main())
""")

    async def child():
        return await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                str(script),
                str(runtime.settings.data_dir),
                team.workspace,
                run.run_id,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "SCHOLARRAG_LANGFUSE_ENABLED": "false"},
        )

    waiting = asyncio.create_task(
        runtime.workspace_queries.store(team.workspace).wait_for_events(
            run.run_id,
            after_seq=run.event_count,
            timeout=10,
        )
    )
    results = await asyncio.gather(child(), child())
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    assert (runtime.settings.data_dir / "fake-model-calls.txt").read_text().splitlines() == [
        "called"
    ]
    assert (await waiting)[0].type == "run.started"
    final = runtime.workspace_queries.read(team.owner, run.run_id)
    assert final.answer.answer == "child process answer"
    assert (
        runtime.workspace_queries.store(team.workspace).list_events(run.run_id)[-1].type
        == "run.completed"
    )


@pytest.mark.parametrize("kind", ["query", "ingestion", "evaluation"])
async def test_actual_cli_worker_handles_sigterm_without_starting_http(team, tmp_path, kind):
    script = tmp_path / "cli_smoke.py"
    ready = tmp_path / "ready"
    script.write_text("""import socket, sys
from pathlib import Path
from scholarrag.config import Settings
Settings.model_config["env_file"] = None
from scholarrag.worker import QueryWorker
from scholarrag.cli import app

def deny(*args, **kwargs):
    raise RuntimeError("Network disabled")
socket.socket.connect = deny
from scholarrag.ingestion.worker import IngestionWorker
from scholarrag.evaluation.worker import EvaluationWorker
worker_type = {"query": QueryWorker, "ingestion": IngestionWorker, "evaluation": EvaluationWorker}[sys.argv[2]]
original = worker_type.run
async def marked(self):
    Path(sys.argv[1]).touch()
    await original(self)
worker_type.run = marked
app(args=["worker", "--kind", sys.argv[2]])
""")
    process = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, str(script), str(ready), kind],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "SCHOLARRAG_DATA_DIR": str(team.settings.data_dir),
            "SCHOLARRAG_AUTH_MODE": "session",
            "SCHOLARRAG_QDRANT_URL": "http://127.0.0.1:9",
            "SCHOLARRAG_LANGFUSE_ENABLED": "false",
        },
    )
    try:
        for _ in range(200):
            if ready.exists() or process.poll() is not None:
                break
            await asyncio.sleep(0.02)
        assert ready.exists(), "CLI did not start its worker"
        process.terminate()
        stdout, stderr = await asyncio.to_thread(process.communicate, timeout=5)
        assert process.returncode == 0, stdout + stderr
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.communicate, timeout=5)


def test_worker_cli_rejects_local_mode_and_embedded_qdrant(team, monkeypatch):
    from typer.testing import CliRunner

    from scholarrag import cli

    monkeypatch.setattr(
        cli, "get_settings", lambda: team.settings.model_copy(update={"auth_mode": "local"})
    )
    result = CliRunner().invoke(cli.app, ["worker"])
    assert result.exit_code == 2 and "AUTH_MODE=session" in result.output
    monkeypatch.setattr(
        cli, "get_settings", lambda: team.settings.model_copy(update={"qdrant_url": None})
    )
    result = CliRunner().invoke(cli.app, ["worker"])
    assert result.exit_code == 2 and "QDRANT_URL" in result.output


async def test_admission_failure_rolls_back_snapshot_ownership_and_audit(team, monkeypatch):
    runtime = Runtime(team.settings)

    def fail_event(*args, **kwargs):
        raise OSError("simulated event write failure")

    monkeypatch.setattr(QueryRunStore, "_event", fail_event)
    with pytest.raises(OSError):
        await runtime.workspace_queries.create(
            team.owner,
            QueryRequest(question="atomic admission", document_ids=[]),
        )
    assert runtime.workspace_queries.store(team.workspace).list() == []
    assert team.store.owned_query_ids(team.owner) == []
    with team.store._transaction(write=False) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM auth_audit WHERE action='query.created'"
            ).fetchone()[0]
            == 0
        )


async def test_concurrent_api_admissions_share_atomic_workspace_capacity(team):
    settings = team.settings.model_copy(update={"workspace_query_max_active": 1})
    runtimes = [Runtime(settings), Runtime(settings)]

    def submit(index):
        try:
            return asyncio.run(
                runtimes[index].workspace_queries.create(
                    team.owner,
                    QueryRequest(question=f"request {index}", document_ids=[]),
                )
            )
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(2) as pool:
        results = await asyncio.to_thread(lambda: list(pool.map(submit, [0, 1])))
    assert results.count(429) == 1
    assert len(team.store.owned_query_ids(team.owner)) == 1
    assert len(runtimes[0].workspace_queries.store(team.workspace).list()) == 1


async def test_worker_reclaiming_own_expired_lease_stops_old_executor(team, monkeypatch):
    runtime, run = await queued(team)
    worker = QueryWorker(runtime)
    started = asyncio.Event()
    calls = []

    async def blocked(*args, **kwargs):
        calls.append(1)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RAGService, "answer", blocked)
    await worker.tick()
    await asyncio.wait_for(started.wait(), 1)
    previous = worker.active[run.run_id]
    expire(worker.stores[run.run_id], run.run_id)
    await worker.tick()
    assert previous.done()
    await asyncio.gather(*worker.active.values())
    final = runtime.workspace_queries.read(team.owner, run.run_id)
    assert final.status == "failed" and calls == [1]
