"""Recovery and publication boundaries; all provider responses are synthetic."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_pipeline import make_mineru_fixture
from test_workspace_identity import team as team  # noqa: PLC0414
from test_workspace_ingestion import enqueue
from test_workspace_ingestion import queue as queue  # noqa: PLC0414

from scholarrag.authorization import bind_authorization
from scholarrag.domain.models import CanonicalDocument
from scholarrag.ingestion.checkpoints import IngestionJournal, IngestionUncertain
from scholarrag.ingestion.mineru_normalizer import MinerUNormalizer
from scholarrag.ingestion.paddleocr_normalizer import PaddleOCRNormalizer
from scholarrag.ingestion.service import IngestionService
from scholarrag.ingestion.worker import IngestionWorker
from scholarrag.ingestion.workspaces import IngestionGuard, WorkspaceIngestions
from scholarrag.providers.mineru import MinerUClient
from scholarrag.providers.paddleocr import PaddleOCRAPIClient
from scholarrag.rag.runs import LeaseLost
from scholarrag.retrieval.index import VectorIndex, VisualIndex
from scholarrag.runtime import Runtime


@pytest.fixture
def pipeline(monkeypatch):
    calls = {"submit": 0, "get": 0, "wait": 0, "text": 0, "visual": 0}
    result = {"code": 0, "data": {"extract_result": [{"state": "done"}]}}

    async def submit(self, path, *, data_id, on_allocated=None):
        calls["submit"] += 1
        job = SimpleNamespace(batch_id="known-batch", trace_id="synthetic")
        if on_allocated:
            on_allocated(job)
        return job

    async def get(self, batch_id):
        assert batch_id == "known-batch"
        calls["get"] += 1
        return result

    async def wait(self, batch_id):
        calls["wait"] += 1
        return result

    async def download(self, response, destination):
        destination.mkdir(parents=True, exist_ok=True)
        make_mineru_fixture(destination)

    async def text(self, chunks):
        calls["text"] += 1
        return len(chunks)

    async def visual(self, document, chunks):
        calls["visual"] += 1
        return 0

    monkeypatch.setattr(MinerUClient, "submit_local_file", submit)
    monkeypatch.setattr(MinerUClient, "get_batch", get)
    monkeypatch.setattr(MinerUClient, "wait_for_result", wait)
    monkeypatch.setattr(MinerUClient, "download_and_extract", download)
    monkeypatch.setattr(VectorIndex, "index", text)
    monkeypatch.setattr(VisualIndex, "index", visual)
    return calls


def row(team, job_id):
    with team.store._transaction(write=False) as connection:
        return dict(
            connection.execute("SELECT * FROM ingestion_jobs WHERE job_id=?", (job_id,)).fetchone()
        )


def expire(team, job_id):
    with team.store._transaction() as connection:
        connection.execute("UPDATE ingestion_jobs SET lease_until=0 WHERE job_id=?", (job_id,))


def executor(team, job):
    return WorkspaceIngestions(team.settings, team.store, lease_token=job["lease_token"])


def guarded(team, job, bound):
    return bind_authorization(
        IngestionGuard(
            team.store,
            team.owner,
            job["job_id"],
            job["lease_token"],
            suspended=lambda: bound.suspended,
        )
    )


async def settle(team):
    worker = IngestionWorker(Runtime(team.settings))
    await worker.tick()
    await asyncio.gather(*worker.active.values())
    return worker


async def test_full_pipeline_activates_and_publishes_once_after_indexing(team, queue, pipeline):
    created = enqueue(team, queue)
    await settle(team)
    await settle(team)
    final = queue.read(team.owner, created["job_id"])
    assert final["status"] == "ready"
    assert pipeline == {"submit": 1, "get": 0, "wait": 1, "text": 1, "visual": 1}
    assert team.store.document_ids(team.owner) == [final["document_id"]]
    assert team.catalog.get_document(final["document_id"])["parse_id"] == final["parse_id"]
    assert row(team, created["job_id"])["lease_token"] is None
    saved = json.loads(row(team, created["job_id"])["checkpoint"])
    assert saved["mineru"]["batch_id"] == "known-batch"
    assert "checkpoint" not in final and "worker_id" not in final and "lease_token" not in final
    assert "known-batch" not in json.dumps(final)
    with team.store._transaction(write=False) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM auth_audit WHERE action='ingestion.published'"
            ).fetchone()[0]
            == 1
        )


async def test_shutdown_after_supplier_id_resumes_polling_without_resubmission(
    team, queue, pipeline, monkeypatch
):
    created = enqueue(team, queue)
    started = asyncio.Event()
    original = MinerUClient.wait_for_result

    async def waiting(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(MinerUClient, "wait_for_result", waiting)
    worker = IngestionWorker(Runtime(team.settings))
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert row(team, created["job_id"])["status"] == "parsing"
    assert (
        json.loads(row(team, created["job_id"])["checkpoint"])["mineru"]["batch_id"]
        == "known-batch"
    )
    monkeypatch.setattr(MinerUClient, "wait_for_result", original)
    expire(team, created["job_id"])
    await settle(team)
    assert queue.read(team.owner, created["job_id"])["status"] == "ready"
    assert pipeline["submit"] == 1 and pipeline["get"] == 1


@pytest.mark.parametrize(
    "state,expected",
    [("submitting", "external_outcome_unknown"), ("allocated", "external_outcome_unknown")],
)
async def test_uncertain_submission_or_missing_upload_is_not_replayed(
    team, queue, pipeline, monkeypatch, state, expected
):
    created = enqueue(team, queue)
    job = queue.claim_next()
    bound = executor(team, job)
    bound.save_checkpoint(job["job_id"], "mineru", {"state": state, "batch_id": "known-batch"})
    if state == "allocated":

        async def waiting_file(*args):
            return {"code": 0, "data": {"extract_result": [{"state": "waiting-file"}]}}

        monkeypatch.setattr(MinerUClient, "get_batch", waiting_file)
    expire(team, job["job_id"])
    await settle(team)
    assert queue.read(team.owner, created["job_id"])["error_code"] == expected
    assert pipeline["submit"] == pipeline["text"] == 0


async def test_allocated_batch_that_received_upload_can_resume(team, queue, pipeline):
    created = enqueue(team, queue)
    job = queue.claim_next()
    executor(team, job).save_checkpoint(
        job["job_id"], "mineru", {"state": "allocated", "batch_id": "known-batch"}
    )
    expire(team, job["job_id"])
    await settle(team)
    assert queue.read(team.owner, created["job_id"])["status"] == "ready"
    assert pipeline["submit"] == 0 and pipeline["get"] == 1


async def test_expired_executor_cannot_checkpoint_fail_or_publish(team, queue):
    created = enqueue(team, queue)
    old = queue.claim_next()
    bound = executor(team, old)
    expire(team, created["job_id"])
    new = queue.claim_next("new-worker")
    assert new["lease_token"] != old["lease_token"]
    for operation in (
        lambda: bound.save_checkpoint(old["job_id"], "mineru", {"state": "submitting"}),
        lambda: bound.fail(old["job_id"], "stale"),
        lambda: bound.publish(team.owner, old["job_id"], old["document_id"], "stale"),
    ):
        with pytest.raises(LeaseLost):
            operation()
    assert row(team, old["job_id"])["lease_token"] == new["lease_token"]


async def test_parsed_candidate_checkpoint_restores_without_supplier_calls(
    team, queue, pipeline, monkeypatch
):
    created = enqueue(team, queue)
    job = queue.claim_next()
    bound = executor(team, job)
    service = IngestionService(
        team.settings,
        document_id=job["document_id"],
        journal=IngestionJournal(bound, job["job_id"]),
    )
    with guarded(team, job, bound):
        candidate = await service._parse_mineru_candidate(Path(job["source_path"]))
    assert candidate.document.document_id == job["document_id"]
    expire(team, job["job_id"])
    await settle(team)
    assert queue.read(team.owner, created["job_id"])["status"] == "ready"
    assert pipeline["submit"] == 1 and pipeline["get"] == pipeline["wait"] - 1 == 0


async def test_artifact_tamper_blocks_checkpoint_restore(team, queue, pipeline):
    created = enqueue(team, queue)
    job = queue.claim_next()
    bound = executor(team, job)
    service = IngestionService(
        team.settings,
        document_id=job["document_id"],
        journal=IngestionJournal(bound, job["job_id"]),
    )
    with guarded(team, job, bound):
        candidate = await service._parse_mineru_candidate(Path(job["source_path"]))
    (candidate.parse_dir / "chunks/chunks.json").write_text("[]")
    expire(team, job["job_id"])
    await settle(team)
    assert queue.read(team.owner, created["job_id"])["status"] == "failed"
    assert pipeline["submit"] == 1 and pipeline["text"] == 0


async def test_completed_index_checkpoints_skip_embedding_on_publication_recovery(
    team, queue, pipeline, monkeypatch
):
    created = enqueue(team, queue)
    job = queue.claim_next()
    bound = executor(team, job)

    # Stop after durable index checkpoints, before the publication transaction.
    def crash(*args, **kwargs):
        bound.suspended = True
        raise LeaseLost("simulated worker crash")

    monkeypatch.setattr(bound, "publish", crash)
    await bound.execute(job)
    assert pipeline["text"] == pipeline["visual"] == 1
    assert team.catalog.get_document(job["document_id"]) is None
    assert team.store.document_ids(team.owner) == []
    expire(team, job["job_id"])
    await settle(team)
    assert queue.read(team.owner, created["job_id"])["status"] == "ready"
    assert pipeline["submit"] == pipeline["text"] == pipeline["visual"] == 1


async def test_interrupted_index_is_not_automatically_reembedded(
    team, queue, pipeline, monkeypatch
):
    created = enqueue(team, queue)
    started = asyncio.Event()

    async def blocking(*args):
        pipeline["text"] += 1
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(VectorIndex, "index", blocking)
    worker = IngestionWorker(Runtime(team.settings))
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    expire(team, created["job_id"])
    await settle(team)
    assert queue.read(team.owner, created["job_id"])["error_code"] == "external_outcome_unknown"
    assert pipeline["text"] == pipeline["submit"] == 1


async def test_publication_transaction_failure_rolls_back_catalog_and_grant(team, queue, pipeline):
    created = enqueue(team, queue)
    with team.store._transaction() as connection:
        connection.execute("""CREATE TRIGGER fail_publication BEFORE INSERT ON auth_audit
            WHEN NEW.action='ingestion.published' BEGIN SELECT RAISE(ABORT,'publication failed'); END""")
    await settle(team)
    final = queue.read(team.owner, created["job_id"])
    assert final["status"] == "failed"
    assert team.store.document_ids(team.owner) == []
    with team.store._transaction(write=False) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM auth_resources").fetchone()[0] == 0


async def test_cancel_while_waiting_prevents_late_publication(team, queue, pipeline, monkeypatch):
    created = enqueue(team, queue)
    started = asyncio.Event()

    async def waiting(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(MinerUClient, "wait_for_result", waiting)
    worker = IngestionWorker(Runtime(team.settings))
    await worker.tick()
    await asyncio.wait_for(started.wait(), 1)
    queue.cancel(team.owner, created["job_id"])
    await asyncio.wait_for(asyncio.gather(*worker.active.values()), 1)
    assert queue.read(team.owner, created["job_id"])["status"] == "canceled"
    assert team.store.document_ids(team.owner) == [] and pipeline["text"] == 0


async def test_changed_configuration_does_not_reuse_old_index_checkpoints(team, queue, pipeline):
    created = enqueue(team, queue)
    job = queue.claim_next()
    bound = executor(team, job)
    with guarded(team, job, bound):
        IngestionService(
            team.settings,
            document_id=job["document_id"],
            journal=IngestionJournal(bound, job["job_id"]),
        )._configuration_checkpoint()
    team.settings.embedding_dimensions += 1
    expire(team, job["job_id"])
    await settle(team)
    assert queue.read(team.owner, created["job_id"])["status"] == "failed"
    assert pipeline["submit"] == 0


async def test_paddle_job_id_resumes_without_submitting_again(team, queue, monkeypatch):
    enqueue(team, queue)
    job = queue.claim_next()
    bound = executor(team, job)
    calls = {"submit": 0, "wait": 0}
    waiting = asyncio.Event()
    monkeypatch.setattr(PaddleOCRAPIClient, "document_options", lambda *args: (None, {}))

    async def submit(*args, **kwargs):
        calls["submit"] += 1
        return SimpleNamespace(job_id="known-paddle-job")

    async def blocked(*args):
        waiting.set()
        await asyncio.Event().wait()

    async def resources(*args):
        return []

    def normalize(self, payload, **kwargs):
        return CanonicalDocument(
            document_id=kwargs["document_id"],
            parse_id=kwargs["parse_id"],
            source_sha256=kwargs["source_sha256"],
            source_filename="synthetic.pdf",
            provider="paddleocr",
            provider_model="synthetic",
            pages=[],
            blocks=[],
        )

    monkeypatch.setattr(PaddleOCRAPIClient, "submit_document", submit)
    monkeypatch.setattr(PaddleOCRAPIClient, "wait_document", blocked)
    monkeypatch.setattr(PaddleOCRAPIClient, "save_document_resources", resources)
    monkeypatch.setattr(PaddleOCRNormalizer, "normalize", normalize)
    with guarded(team, job, bound):
        service = IngestionService(
            team.settings,
            document_id=job["document_id"],
            journal=IngestionJournal(bound, job["job_id"]),
        )
        task = asyncio.create_task(service._parse_paddle_candidate(Path(job["source_path"])))
        await asyncio.wait_for(waiting.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    expire(team, job["job_id"])
    resumed = queue.claim_next("restarted")
    new_bound = executor(team, resumed)

    async def result(self, job_id):
        assert job_id == "known-paddle-job"
        calls["wait"] += 1
        return SimpleNamespace(pages=[])

    monkeypatch.setattr(PaddleOCRAPIClient, "wait_document", result)
    with guarded(team, resumed, new_bound):
        service = IngestionService(
            team.settings,
            document_id=job["document_id"],
            journal=IngestionJournal(new_bound, job["job_id"]),
        )
        first = await service._parse_paddle_candidate(Path(job["source_path"]))
        second = await service._parse_paddle_candidate(Path(job["source_path"]))
    assert first.document.parse_id == second.document.parse_id
    assert calls == {"submit": 1, "wait": 1}


async def test_uncertain_paddle_submit_cannot_be_replayed(team, queue):
    enqueue(team, queue)
    job = queue.claim_next()
    bound = executor(team, job)
    bound.save_checkpoint(job["job_id"], "paddleocr", {"state": "submitting"})
    with guarded(team, job, bound), pytest.raises(IngestionUncertain):
        service = IngestionService(
            team.settings,
            document_id=job["document_id"],
            journal=IngestionJournal(bound, job["job_id"]),
        )
        await service._parse_paddle_candidate(Path(job["source_path"]))


async def test_lease_renews_during_slow_local_normalization(team, queue, pipeline, monkeypatch):
    team.settings.ingestion_worker_lease_seconds = 1
    created = enqueue(team, queue)
    original = MinerUNormalizer.normalize

    def slow(self, *args, **kwargs):
        time.sleep(1.2)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MinerUNormalizer, "normalize", slow)
    await settle(team)
    assert queue.read(team.owner, created["job_id"])["status"] == "ready"


async def test_resumed_job_rechecks_revoked_session_before_supplier_access(team, queue, pipeline):
    created = enqueue(team, queue)
    job = queue.claim_next()
    executor(team, job).save_checkpoint(
        job["job_id"], "mineru", {"state": "submitted", "batch_id": "known-batch"}
    )
    team.store.revoke_session(team.owner.session_id)
    expire(team, job["job_id"])
    await settle(team)
    assert row(team, created["job_id"])["error_code"] == "authorization_revoked"
    assert pipeline["submit"] == pipeline["get"] == 0


async def test_two_real_processes_resume_one_supplier_task_and_publish_once(team, queue, tmp_path):
    created = enqueue(team, queue)
    job = queue.claim_next()
    executor(team, job).save_checkpoint(
        job["job_id"], "mineru", {"state": "submitted", "batch_id": "known-batch"}
    )
    expire(team, job["job_id"])
    fixture = tmp_path / "provider-output"
    fixture.mkdir()
    make_mineru_fixture(fixture)
    script = tmp_path / "ingestion_child.py"
    script.write_text("""import asyncio, socket, sys, shutil
from pathlib import Path
from scholarrag.config import Settings
from scholarrag.runtime import Runtime
from scholarrag.ingestion.worker import IngestionWorker
from scholarrag.providers.mineru import MinerUClient
from scholarrag.retrieval.index import VectorIndex, VisualIndex

def deny(*args, **kwargs):
    raise RuntimeError("Network disabled")
socket.socket.connect = deny
async def no_submit(*args, **kwargs):
    raise AssertionError("Must not submit the saved supplier task again")
async def get(self, batch_id):
    assert batch_id == "known-batch"
    return {"code": 0, "data": {"extract_result": [{"state": "done"}]}}
async def download(self, result, destination):
    shutil.copytree(sys.argv[2], destination, dirs_exist_ok=True)
async def text(self, chunks):
    with (self.settings.data_dir / "fake-embedding-calls.txt").open("a") as stream:
        stream.write("indexed\\n")
    await asyncio.sleep(0.1)
    return len(chunks)
async def visual(self, document, chunks):
    return 0
MinerUClient.submit_local_file = no_submit
MinerUClient.get_batch = get
MinerUClient.download_and_extract = download
VectorIndex.index = text
VisualIndex.index = visual
async def main():
    settings = Settings(_env_file=None, data_dir=Path(sys.argv[1]), auth_mode="session", parse_fallback_mode="off")
    runtime = Runtime(settings)
    worker = IngestionWorker(runtime)
    for _ in range(200):
        await worker.tick()
        with runtime.identities._transaction(write=False) as connection:
            status = connection.execute("SELECT status FROM ingestion_jobs WHERE job_id=?", (sys.argv[3],)).fetchone()[0]
        if status in {"ready", "failed"}:
            break
        await asyncio.sleep(0.02)
    await asyncio.gather(*worker.active.values())
    assert status == "ready", status
asyncio.run(main())
""")

    async def child():
        return await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                str(script),
                str(team.settings.data_dir),
                str(fixture),
                created["job_id"],
            ],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "SCHOLARRAG_LANGFUSE_ENABLED": "false"},
        )

    results = await asyncio.gather(child(), child())
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    assert queue.read(team.owner, created["job_id"])["status"] == "ready"
    assert (team.settings.data_dir / "fake-embedding-calls.txt").read_text().splitlines() == [
        "indexed"
    ]
    with team.store._transaction(write=False) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM auth_audit WHERE action='ingestion.published'"
            ).fetchone()[0]
            == 1
        )


async def test_supplier_timeout_without_id_is_recorded_as_unknown_not_retryable(
    team, queue, pipeline, monkeypatch
):
    created = enqueue(team, queue)

    async def timeout(*args, **kwargs):
        pipeline["submit"] += 1
        raise TimeoutError("synthetic lost response")

    monkeypatch.setattr(MinerUClient, "submit_local_file", timeout)
    await settle(team)
    await settle(team)
    assert queue.read(team.owner, created["job_id"])["error_code"] == "external_outcome_unknown"
    assert json.loads(row(team, created["job_id"])["checkpoint"])["mineru"]["state"] == "submitting"
    assert pipeline["submit"] == 1
