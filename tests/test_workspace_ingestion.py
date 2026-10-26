from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_pipeline import make_mineru_fixture
from test_upload_limits import pdf_bytes
from test_workspace_identity import add_document
from test_workspace_identity import team as team  # noqa: PLC0414
from typer.testing import CliRunner

from scholarrag.api.main import create_app
from scholarrag.authorization import authorized_documents, bind_authorization
from scholarrag.domain.access import AccessDenied, IdentityConflict, Role
from scholarrag.domain.models import CanonicalDocument
from scholarrag.ingestion.service import IngestionService
from scholarrag.ingestion.workspaces import IngestionGuard, WorkspaceIngestions
from scholarrag.providers.mineru import MinerUClient
from scholarrag.retrieval.index import VectorIndex, VisualIndex
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.identity import IdentityStore

SYNTHETIC_PDF = pdf_bytes(2)


@pytest.fixture
def queue(team):
    team.settings.authorization_poll_seconds = 0.1
    team.settings.parse_fallback_mode = "off"
    return WorkspaceIngestions(team.settings, team.store)


def enqueue(team, queue, *, context=None, key="request", data=None):
    path = team.settings.data_dir / f"upload-{uuid.uuid4().hex}.pdf"
    path.write_bytes(data or SYNTHETIC_PDF)
    result, created = queue.enqueue(
        context or team.owner,
        filename="paper.pdf",
        source_path=path,
        source_sha256=ArtifactStore.sha256(path),
        idempotency_key=key,
    )
    if not created:
        path.unlink()
    return result


def register(team, job):
    document = CanonicalDocument(
        document_id=job["document_id"],
        parse_id=f"parse-{uuid.uuid4().hex}",
        source_filename="paper.pdf",
        source_sha256=job["source_sha256"],
        provider="test",
        provider_model="test",
    )
    team.catalog.register(
        document,
        [],
        source_path=job["source_path"],
        artifact_dir=team.settings.artifacts_dir,
    )
    return document


def test_idempotency_is_private_and_content_hash_is_not_identity(team, queue):
    data = pdf_bytes()
    first = enqueue(team, queue, data=data)
    assert enqueue(team, queue, data=data)["job_id"] == first["job_id"]
    member = team.store.context_for(team.principals["member"], team.workspace)
    other = team.store.context_for(team.principals["other_owner"], team.other)
    second = enqueue(team, queue, context=member, data=data)
    third = enqueue(team, queue, context=other, data=data)
    assert len({first["job_id"], second["job_id"], third["job_id"]}) == 3
    jobs = [queue.claim_next() for _ in range(3)]
    assert len({job["document_id"] for job in jobs}) == 3
    assert all(len(job["document_id"]) == 36 for job in jobs)
    assert len({job["source_sha256"] for job in jobs}) == 1
    with pytest.raises(IdentityConflict, match="different PDF"):
        enqueue(team, queue, data=b"different bytes")
    public = json.dumps(queue.read(team.owner, first["job_id"]))
    for secret in (jobs[0]["source_sha256"], jobs[0]["source_path"], team.owner.session_id):
        assert secret not in public


def test_private_jobs_and_events_deny_admin_and_other_workspace(team, queue):
    job = enqueue(team, queue)
    for name, ws in (
        ("admin", team.workspace),
        ("member", team.workspace),
        ("other_owner", team.other),
    ):
        context = team.store.context_for(team.principals[name], ws)
        assert queue.list(context) == []
        for operation in (queue.read, queue.events, queue.cancel):
            with pytest.raises(AccessDenied):
                operation(context, job["job_id"])
    assert queue.events(team.owner, job["job_id"])[0]["status"] == "queued"


def test_concurrent_idempotency_and_claim_are_atomic(team, queue):
    data = pdf_bytes()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: enqueue(team, queue, data=data), range(4)))
        claimed = list(pool.map(lambda _: queue.claim_next(), range(4)))
    assert len({job["job_id"] for job in results}) == 1
    assert sum(job is not None for job in claimed) == 1
    assert [row["status"] for row in queue.events(team.owner, results[0]["job_id"])] == [
        "queued",
        "parsing",
    ]


def test_capacity_rejects_atomically_but_allows_idempotent_replay(team, queue):
    team.settings.workspace_ingestion_max_per_user = 1
    first = enqueue(team, queue)
    assert enqueue(team, queue)["job_id"] == first["job_id"]
    with pytest.raises(IdentityConflict, match="capacity"):
        enqueue(team, queue, key="new")
    queue.cancel(team.owner, first["job_id"])
    assert enqueue(team, queue, key="new")["status"] == "queued"


def test_publish_is_private_and_requires_active_matching_content(team, queue):
    created = enqueue(team, queue)
    job = queue.claim_next()
    with pytest.raises(IdentityConflict, match="not active"):
        queue.publish(team.owner, job["job_id"], job["document_id"], "missing")
    document = register(team, job)
    assert team.store.document_ids(team.owner) == []
    queue.publish(team.owner, job["job_id"], document.document_id, document.parse_id)
    assert queue.read(team.owner, created["job_id"])["status"] == "ready"
    assert team.store.document_ids(team.owner) == [document.document_id]
    admin = team.store.context_for(team.principals["admin"], team.workspace)
    assert team.store.document_ids(admin) == []
    team.store.unassign_document(document.document_id, team.workspace)
    assert queue.read(team.owner, job["job_id"])["document_id"] is None


def test_publish_failure_rolls_back_grant_state_events_and_audit(team, queue):
    enqueue(team, queue)
    job = queue.claim_next()
    document = register(team, job)
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        connection.execute("""CREATE TRIGGER publication_abort BEFORE INSERT ON auth_audit
          WHEN NEW.action='ingestion.published' BEGIN SELECT RAISE(ABORT,'test rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        queue.publish(team.owner, job["job_id"], document.document_id, document.parse_id)
    assert team.store.document_ids(team.owner) == []
    assert queue.read(team.owner, job["job_id"])["status"] == "parsing"
    assert [event["status"] for event in queue.events(team.owner, job["job_id"])] == [
        "queued",
        "parsing",
    ]
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        assert connection.execute("SELECT parse_id FROM ingestion_jobs").fetchone()[0] is None


def test_cancel_and_recovery_never_replay_paid_tasks(team, queue):
    first = enqueue(team, queue)
    second = enqueue(team, queue, key="second")
    queue.cancel(team.owner, second["job_id"])
    job = queue.claim_next()
    assert job["job_id"] == first["job_id"]
    queue.recover()
    queue.recover()
    assert queue.claim_next() is None
    assert queue.read(team.owner, first["job_id"])["error_code"] == "execution_interrupted"
    assert len(queue.events(team.owner, first["job_id"])) == 3
    assert enqueue(team, queue)["status"] == "failed"


@pytest.mark.parametrize("revocation", ["session", "membership", "downgrade"])
async def test_revoked_queued_actor_never_calls_parser(team, queue, monkeypatch, revocation):
    member = team.store.context_for(team.principals["member"], team.workspace)
    enqueue(team, queue, context=member)
    job = queue.claim_next()
    if revocation == "session":
        team.store.revoke_session(member.session_id)
    elif revocation == "membership":
        team.store.remove_member(team.owner, member.user_id)
    else:
        team.store.set_member(team.owner, member.user_id, Role.VIEWER)

    async def forbidden(*args, **kwargs):
        pytest.fail("revoked actor reached parser")

    monkeypatch.setattr(IngestionService, "ingest", forbidden)
    await queue.execute(job)
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        assert connection.execute("SELECT status,error_code FROM ingestion_jobs").fetchone() == (
            "failed",
            "authorization_revoked",
        )


async def test_worker_cancels_inflight_job_and_continues_next_job(team, queue, monkeypatch):
    first = enqueue(team, queue)
    second = enqueue(team, queue, key="second")
    started, completed = asyncio.Event(), asyncio.Event()

    async def ingest(service, path):
        with sqlite3.connect(team.settings.sqlite_path) as connection:
            connection.row_factory = sqlite3.Row
            row = dict(
                connection.execute(
                    "SELECT * FROM ingestion_jobs WHERE document_id=?", (service.document_id,)
                ).fetchone()
            )
        if row["job_id"] == first["job_id"]:
            started.set()
            await asyncio.Event().wait()
        document = register(team, row)
        completed.set()
        return document, [], team.settings.artifacts_dir

    monkeypatch.setattr(IngestionService, "ingest", ingest)
    worker = asyncio.create_task(queue.worker())
    try:
        await asyncio.wait_for(started.wait(), 3)
        queue.cancel(team.owner, first["job_id"])
        await asyncio.wait_for(completed.wait(), 3)
        # execute() publishes synchronously immediately after the test stub returns.
        assert queue.read(team.owner, second["job_id"])["status"] == "ready"
        assert queue.read(team.owner, first["job_id"])["status"] == "canceled"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_real_ingestion_preserves_allocated_id_and_source_artifact(team, queue, monkeypatch):
    created = enqueue(team, queue)
    job = queue.claim_next()
    submitted, indexed = [], []
    errors = []
    original_ingest = IngestionService.ingest

    async def traced_ingest(*args, **kwargs):
        try:
            return await original_ingest(*args, **kwargs)
        except Exception as exc:
            errors.append(repr(exc))
            raise

    monkeypatch.setattr(IngestionService, "ingest", traced_ingest)

    async def submit(self, path, *, data_id):
        submitted.append(data_id)
        return SimpleNamespace(batch_id="synthetic", trace_id="synthetic")

    async def wait(*args, **kwargs):
        return {"code": 0, "data": {"extract_result": [{"state": "done"}]}}

    async def download(self, result, destination):
        destination.mkdir(parents=True, exist_ok=True)
        make_mineru_fixture(destination)

    async def index(self, chunks):
        assert authorized_documents(team.settings, None) == []
        indexed.extend(chunk.document_id for chunk in chunks)
        return len(chunks)

    async def visual(self, document, chunks):
        return 0

    monkeypatch.setattr(MinerUClient, "submit_local_file", submit)
    monkeypatch.setattr(MinerUClient, "wait_for_result", wait)
    monkeypatch.setattr(MinerUClient, "download_and_extract", download)
    monkeypatch.setattr(VectorIndex, "index", index)
    monkeypatch.setattr(VisualIndex, "index", visual)
    await queue.execute(job)
    result = queue.read(team.owner, created["job_id"])
    assert result["status"] == "ready", errors
    assert submitted == [job["document_id"]]
    assert indexed and set(indexed) == {job["document_id"]}
    assert not Path(job["source_path"]).exists()
    source = team.catalog.get_document(job["document_id"])["source_path"]
    assert ArtifactStore.sha256(Path(source)) == job["source_sha256"]


async def test_mutated_source_is_rejected_before_provider(team, queue, monkeypatch):
    enqueue(team, queue)
    job = queue.claim_next()

    Path(job["source_path"]).write_bytes(b"replaced")

    async def forbidden(*args, **kwargs):
        pytest.fail("mutated source reached parser")

    monkeypatch.setattr(IngestionService, "ingest", forbidden)
    await queue.execute(job)
    assert queue.read(team.owner, job["job_id"])["error_code"] == "ingestion_failed"


async def test_mineru_checks_guard_before_each_remote_request(team, queue):
    enqueue(team, queue)
    job = queue.claim_next()
    called = []

    def handler(request):
        called.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"extract_result": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = MinerUClient(team.settings, client=client)
    with bind_authorization(IngestionGuard(team.store, team.owner, job["job_id"])):
        await provider.get_batch("synthetic")
        team.store.revoke_session(team.owner.session_id)
        with pytest.raises(AccessDenied):
            await provider.get_batch("synthetic")
    await client.aclose()
    assert len(called) == 1


async def test_upload_api_enforces_scope_key_and_hides_internal_fields(team):
    app = create_app(team.settings)
    prefix = f"/api/workspaces/{team.workspace}"
    headers = {
        "Authorization": f"Bearer {team.sessions['owner'][1]}",
        "Idempotency-Key": "api-request",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
    ) as client:
        created = await client.post(f"{prefix}/uploads", files={"file": ("paper.pdf", pdf_bytes())})
        assert created.status_code == 202 and "no-store" in created.headers["cache-control"]
        job_id = created.json()["job_id"]
        assert "source_path" not in created.json() and "actor_session_id" not in created.json()
        conflict = await client.post(
            f"{prefix}/uploads", files={"file": ("paper.pdf", pdf_bytes(2))}
        )
        assert conflict.status_code == 409
        assert (
            len(
                list((team.settings.data_dir / "workspaces" / team.workspace / "uploads").glob("*"))
            )
            == 1
        )
        assert len((await client.get(f"{prefix}/ingestion-jobs")).json()) == 1
        assert (await client.get(f"{prefix}/ingestion-jobs?limit=0")).status_code == 422
        client.headers["Authorization"] = f"Bearer {team.sessions['admin'][1]}"
        assert (await client.get(f"{prefix}/ingestion-jobs/{job_id}")).status_code == 403
        client.headers["Authorization"] = f"Bearer {team.sessions['viewer'][1]}"
        assert (
            await client.post(f"{prefix}/uploads", files={"file": ("paper.pdf", pdf_bytes())})
        ).status_code == 403
        client.headers["Authorization"] = headers["Authorization"]
        assert (await client.post(f"{prefix}/ingestion-jobs/{job_id}/cancel")).json()[
            "status"
        ] == "canceled"
        assert (await client.get(f"{prefix}/ingestion-jobs/{job_id}/event-log")).json()[-1][
            "status"
        ] == "canceled"
    paths = app.openapi()["paths"]
    assert "/api/jobs" not in paths and "post" not in paths["/api/documents"]


def test_v3_migration_discards_legacy_tasks_but_preserves_documents_and_users(team):
    add_document(team, "preserved")
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        connection.execute("DROP TABLE ingestion_job_events")
        connection.execute("DROP TABLE ingestion_jobs")
        connection.execute("DELETE FROM auth_schema_versions WHERE version=4")
        connection.execute("CREATE TABLE ingestion_jobs(job_id TEXT PRIMARY KEY,status TEXT)")
        connection.execute(
            "CREATE TABLE ingestion_job_events(job_id TEXT REFERENCES ingestion_jobs(job_id),seq INTEGER)"
        )
        connection.execute("INSERT INTO ingestion_jobs VALUES('obsolete','queued')")
        connection.execute("INSERT INTO ingestion_job_events VALUES('obsolete',1)")
    upgraded = IdentityStore(team.settings)
    IdentityStore(team.settings)
    assert upgraded.authenticate(team.sessions["owner"][1]).user_id == team.owner.user_id
    assert team.catalog.get_document("preserved") is not None
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ingestion_job_events").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO ingestion_jobs(job_id) VALUES('anonymous')")
        assert (
            connection.execute("SELECT MAX(version) FROM auth_schema_versions").fetchone()[0] == 4
        )


def test_cli_ingest_submits_to_unified_api_without_direct_parser(team, monkeypatch):
    from scholarrag.cli import app

    monkeypatch.setenv("SCHOLARRAG_SESSION_TOKEN", "test-user-session")
    path = team.settings.data_dir / "cli.pdf"
    path.write_bytes(pdf_bytes())
    calls = []
    original = httpx.Client

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer test-user-session"
        assert request.headers["idempotency-key"] == "cli-key"
        assert request.url.path == f"/api/workspaces/{team.workspace}/uploads"
        return httpx.Response(202, json={"job_id": "job-test", "status": "queued"})

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    result = CliRunner().invoke(
        app, ["ingest", str(path), "--workspace", team.workspace, "--idempotency-key", "cli-key"]
    )
    assert result.exit_code == 0, result.output
    assert "test-user-session" not in result.output and "job-test" in result.output
    assert len(calls) == 1


async def test_final_authorization_rejects_late_result(team, queue, monkeypatch):
    enqueue(team, queue)
    job = queue.claim_next()

    async def ingest(service, path):
        document = register(team, job)
        team.store.revoke_session(team.owner.session_id)
        return document, [], team.settings.artifacts_dir

    monkeypatch.setattr(IngestionService, "ingest", ingest)
    await queue.execute(job)
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        assert connection.execute("SELECT status,error_code FROM ingestion_jobs").fetchone() == (
            "failed",
            "authorization_revoked",
        )
        assert connection.execute("SELECT COUNT(*) FROM auth_resources").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM ingestion_job_events WHERE status='ready'"
            ).fetchone()[0]
            == 0
        )


async def test_provider_error_is_redacted_and_not_automatically_retried(team, queue, monkeypatch):
    enqueue(team, queue)
    job = queue.claim_next()

    async def ingest(*args, **kwargs):
        raise RuntimeError("signed-url?token=synthetic-secret")

    monkeypatch.setattr(IngestionService, "ingest", ingest)
    await queue.execute(job)
    assert queue.read(team.owner, job["job_id"])["error_code"] == "ingestion_failed"
    assert queue.claim_next() is None
    assert "synthetic-secret" not in json.dumps(queue.read(team.owner, job["job_id"]))
    assert "synthetic-secret" not in json.dumps(queue.events(team.owner, job["job_id"]))


async def test_session_lifespan_starts_the_unified_worker(team, monkeypatch):
    app = create_app(team.settings)
    processed = asyncio.Event()

    async def ingest(service, path):
        with sqlite3.connect(team.settings.sqlite_path) as connection:
            connection.row_factory = sqlite3.Row
            job = dict(
                connection.execute(
                    "SELECT * FROM ingestion_jobs WHERE document_id=?", (service.document_id,)
                ).fetchone()
            )
        document = register(team, job)
        processed.set()
        return document, [], team.settings.artifacts_dir

    monkeypatch.setattr(IngestionService, "ingest", ingest)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "Authorization": f"Bearer {team.sessions['owner'][1]}",
                "Idempotency-Key": "lifespan-test",
            },
        ) as client,
    ):
        result = await client.post(
            f"/api/workspaces/{team.workspace}/uploads",
            files={"file": ("paper.pdf", pdf_bytes())},
        )
        assert result.status_code == 202
        await asyncio.wait_for(processed.wait(), 3)
        job_id = result.json()["job_id"]
        assert app.state.runtime.workspace_ingestions.read(team.owner, job_id)["status"] == "ready"
    assert all(task.done() for task in app.state.runtime.tasks)


def test_unsupported_schema_rolls_back_legacy_task_removal(team):
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        connection.execute("DROP TABLE ingestion_job_events")
        connection.execute("DROP TABLE ingestion_jobs")
        connection.execute("CREATE TABLE ingestion_jobs(job_id TEXT)")
        connection.execute("INSERT INTO ingestion_jobs VALUES('preserve-on-failed-migration')")
        connection.execute("INSERT INTO auth_schema_versions VALUES(99,0)")
    with pytest.raises(IdentityConflict, match="Unsupported"):
        IdentityStore(team.settings)
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        assert connection.execute("SELECT * FROM ingestion_jobs").fetchall() == [
            ("preserve-on-failed-migration",)
        ]
