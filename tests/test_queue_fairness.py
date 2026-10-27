"""Real queue admission/claims with persistent fairness; no provider calls."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_workspace_evaluations import parent
from test_workspace_identity import team as team  # noqa: PLC0414
from test_workspace_ingestion import enqueue

from scholarrag.api.schemas import QueryRequest
from scholarrag.evaluation.workspaces import WorkspaceEvaluationRequest
from scholarrag.runtime import Runtime


@pytest.fixture(params=["query", "ingestion", "evaluation"])
def fair_queue(team, request):
    runtime = Runtime(team.settings)
    kind = request.param
    counter = 0

    async def create(context=None):
        nonlocal counter
        counter += 1
        context = context or team.owner
        if kind == "query":
            job = await runtime.workspace_queries.create(
                context,
                QueryRequest(question=f"fairness {counter}", document_ids=[], agentic=False),
            )
            return job.run_id
        if kind == "ingestion":
            job = enqueue(
                team, runtime.workspace_ingestions, context=context, key=f"fairness-{counter}"
            )
            return job["job_id"]
        run = await parent(team, runtime, context)
        job = await runtime.workspace_evaluations.create(
            context, WorkspaceEvaluationRequest(query_run_id=run.run_id), f"fairness-{counter}"
        )
        return job.job_id

    def claim():
        # A fresh runtime cannot reset the persisted order or lose previous leases.
        fresh = Runtime(team.settings)
        store = {
            "query": fresh.query_runs,
            "ingestion": fresh.workspace_ingestions,
            "evaluation": fresh.evaluation_jobs,
        }[kind]
        result = store.claim_next("fairness-test")
        if result is None:
            return None
        if kind == "ingestion":
            return result["job_id"]
        job, _ = result
        return job.run_id if kind == "query" else job.job_id

    def transaction():
        return runtime.identities._transaction()

    return SimpleNamespace(
        create=create,
        claim=claim,
        kind=kind,
        transaction=transaction,
        table={
            "query": "query_run_snapshots",
            "ingestion": "ingestion_jobs",
            "evaluation": "evaluation_job_snapshots",
        }[kind],
        id_column="run_id" if kind == "query" else "job_id",
        other=team.store.context_for(team.principals["other_owner"], team.other),
        member=team.store.context_for(team.principals["member"], team.workspace),
    )


async def test_later_workspace_does_not_wait_for_older_backlog(team, fair_queue):
    q = fair_queue
    first = [await q.create() for _ in range(3)]
    other = [await q.create(q.other) for _ in range(2)]
    assert [q.claim() for _ in range(5)] == [first[0], other[0], first[1], other[1], first[2]]
    assert q.claim() is None
    with q.transaction() as connection:
        assert (
            connection.execute(
                "SELECT turn FROM queue_scheduling_clocks WHERE kind=?", (q.kind,)
            ).fetchone()[0]
            == 5
        )


async def test_users_rotate_within_workspace_but_do_not_multiply_workspace_share(team, fair_queue):
    q = fair_queue
    owner = [await q.create() for _ in range(2)]
    member = [await q.create(q.member) for _ in range(2)]
    other = [await q.create(q.other) for _ in range(2)]
    assert [q.claim() for _ in range(6)] == [
        owner[0],
        other[0],
        member[0],
        other[1],
        owner[1],
        member[1],
    ]


async def test_workspace_arriving_after_claim_gets_next_available_turn(team, fair_queue):
    q = fair_queue
    first = [await q.create() for _ in range(3)]
    assert q.claim() == first[0]
    other = await q.create(q.other)
    assert q.claim() == other
    assert [q.claim(), q.claim()] == first[1:]


async def test_live_leases_are_skipped_and_expired_work_uses_same_rotation(team, fair_queue):
    q = fair_queue
    first = await q.create()
    assert q.claim() == first
    assert q.claim() is None
    other = await q.create(q.other)
    with q.transaction() as connection:
        connection.execute(f"UPDATE {q.table} SET lease_until=0 WHERE {q.id_column}=?", (first,))
    assert q.claim() == other
    assert q.claim() == first
    assert q.claim() is None


async def test_cancelled_backlog_never_uses_a_turn(team, fair_queue):
    q = fair_queue
    first = await q.create()
    other = await q.create(q.other)
    with q.transaction() as connection:
        connection.execute(
            f"UPDATE {q.table} SET status=? WHERE {q.id_column}=?",
            ("canceled" if q.kind == "ingestion" else "cancelled", first),
        )
    assert q.claim() == other
    assert q.claim() is None
    with q.transaction() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM queue_scheduling_turns WHERE kind=? AND workspace_id=?",
                (q.kind, team.workspace),
            ).fetchone()[0]
            == 0
        )


async def test_failed_lease_write_rolls_back_fairness_cursor(team, fair_queue):
    q = fair_queue
    first = await q.create()
    other = await q.create(q.other)
    with q.transaction() as connection:
        connection.execute(
            f"""CREATE TRIGGER reject_claim BEFORE UPDATE OF lease_token ON {q.table}
            BEGIN SELECT RAISE(ABORT,'injected lease failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected lease failure"):
        q.claim()
    with q.transaction() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM queue_scheduling_clocks WHERE kind=?", (q.kind,)
            ).fetchone()[0]
            == 0
        )
        connection.execute("DROP TRIGGER reject_claim")
    assert [q.claim(), q.claim()] == [first, other]


async def test_concurrent_claims_are_unique_and_preserve_fair_turn_count(team, fair_queue):
    q = fair_queue
    expected = {await q.create() for _ in range(3)}
    expected.update({await q.create(q.other) for _ in range(3)})
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: q.claim(), range(8)))
    assert {item for item in results if item} == expected
    assert len([item for item in results if item]) == 6
    with q.transaction() as connection:
        assert (
            connection.execute(
                "SELECT turn FROM queue_scheduling_clocks WHERE kind=?", (q.kind,)
            ).fetchone()[0]
            == 6
        )


async def test_cursor_survives_independent_process_exit(team, fair_queue):
    q = fair_queue
    first = [await q.create() for _ in range(2)]
    other = await q.create(q.other)
    script = """
import json, sys
from pathlib import Path
from scholarrag.config import Settings
from scholarrag.runtime import Runtime
runtime = Runtime(Settings(_env_file=None, data_dir=Path(sys.argv[1]), auth_mode='session'))
kind = sys.argv[2]
store = {'query': runtime.query_runs, 'ingestion': runtime.workspace_ingestions,
         'evaluation': runtime.evaluation_jobs}[kind]
result = store.claim_next('isolated-process')
if kind == 'ingestion':
    identifier = result['job_id']
else:
    job, _ = result
    identifier = job.run_id if kind == 'query' else job.job_id
print(json.dumps(identifier))
"""
    results = []
    environment = dict(os.environ, SCHOLARRAG_DATA_DIR=str(team.settings.data_dir))
    for _ in range(2):
        process = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", script, str(team.settings.data_dir), q.kind],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
            check=True,
        )
        results.append(json.loads(process.stdout))
    assert results == [first[0], other]
    assert q.claim() == first[1]


async def test_other_queue_traffic_cannot_change_this_queue_order(team, fair_queue):
    q = fair_queue
    first = await q.create()
    other = await q.create(q.other)
    foreign_kind = "evaluation" if q.kind == "ingestion" else "ingestion"
    with q.transaction() as connection:
        connection.execute(
            "INSERT INTO queue_scheduling_clocks(kind,turn) VALUES(?,9999)", (foreign_kind,)
        )
        connection.execute(
            "INSERT INTO queue_scheduling_turns VALUES(?,?,?,9999)",
            (foreign_kind, team.workspace, ""),
        )
    assert [q.claim(), q.claim()] == [first, other]
    with q.transaction() as connection:
        assert (
            connection.execute(
                "SELECT turn FROM queue_scheduling_clocks WHERE kind=?", (q.kind,)
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT turn FROM queue_scheduling_clocks WHERE kind=?", (foreign_kind,)
            ).fetchone()[0]
            == 9999
        )
