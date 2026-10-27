"""Scoped parse switching: no indexing, provider calls or client-supplied actor."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from test_pipeline import make_high_quality_document
from test_workspace_identity import add_document
from test_workspace_identity import team as team  # noqa: PLC0414

from scholarrag.api.main import create_app
from scholarrag.domain.access import AccessDenied, IdentityConflict
from scholarrag.ingestion.chunker import SemanticChunker
from scholarrag.ingestion.workspace_reviews import WorkspaceParseActivation, WorkspaceParseReviews
from scholarrag.storage.artifacts import ArtifactStore


@pytest.fixture
def review(team):
    artifacts = ArtifactStore(team.settings)
    documents = []
    for name in ("old", "new"):
        document = make_high_quality_document(parse_id=f"parse_{name}")
        document.title = f"{name} title"
        directory = artifacts.parse_dir(document.document_id, document.parse_id)
        chunks = SemanticChunker(max_chars=10000).chunk(document)
        artifacts.write_json(
            directory / "canonical/document.json", document.model_dump(mode="json")
        )
        artifacts.write_json(
            directory / "chunks/chunks.json", [c.model_dump(mode="json") for c in chunks]
        )
        artifacts.write_json(
            directory / "manifest.json",
            {
                "document_id": document.document_id,
                "parse_id": document.parse_id,
                "provider": document.provider,
                "provider_model": document.provider_model,
                "status": "ready",
                "quality": {"decision": "accept", "score": 1.0},
                "preserved_source": "/secret/private/source.pdf",
            },
        )
        team.catalog.register(
            document, chunks, source_path=directory / "synthetic.pdf", artifact_dir=directory
        )
        documents.append(document)
    document_id = documents[0].document_id
    team.store.assign_document(document_id, team.workspace, team.users["owner"])
    with team.store._transaction() as connection:
        connection.execute(
            "UPDATE auth_resources SET visibility='private' WHERE resource_id=?", (document_id,)
        )
    service = WorkspaceParseReviews(team.settings, team.store)
    return SimpleNamespace(
        service=service,
        document_id=document_id,
        artifacts=artifacts,
        body=WorkspaceParseActivation(
            expected_revision=0, expected_parse_id="parse_new", reason="人工检查通过"
        ),
    )


def shared(team, review):
    with team.store._transaction() as connection:
        connection.execute(
            "UPDATE auth_resources SET visibility='workspace' WHERE resource_id=?",
            (review.document_id,),
        )


def test_snapshot_filters_paths_and_reports_current_version(team, review):
    data = review.service.snapshot(team.owner, review.document_id)
    assert data["active_parse_id"] == "parse_new" and data["revision"] == 0
    assert len(data["versions"]) == 2
    assert "/secret" not in json.dumps(data) and "artifact_dir" not in json.dumps(data)
    assert all(version["can_activate"] for version in data["versions"])


def test_atomic_switch_updates_metadata_and_server_actor(team, review):
    result = review.service.activate(team.owner, review.document_id, "parse_old", review.body)
    assert result["revision"] == 1
    assert team.catalog.active_parse_ids([review.document_id]) == ["parse_old"]
    assert team.catalog.get_document(review.document_id)["title"] == "old title"
    assert (
        team.catalog.get_metadata(review.document_id)["extraction"]["source_parse_id"]
        == "parse_old"
    )
    data = review.service.snapshot(team.owner, review.document_id)
    assert data["history"][0]["actor_id"] == team.users["owner"]
    assert data["history"][0]["reason"] == review.body.reason
    assert data["history"][0]["previous_parse_id"] == "parse_new"


@pytest.mark.parametrize("name", ["member", "viewer"])
def test_members_can_read_shared_versions_but_not_activate(team, review, name):
    shared(team, review)
    context = team.store.context_for(team.principals[name], team.workspace)
    assert review.service.snapshot(context, review.document_id)["versions"]
    with pytest.raises(AccessDenied):
        review.service.activate(context, review.document_id, "parse_old", review.body)


def test_admin_cannot_review_another_users_private_document(team, review):
    context = team.store.context_for(team.principals["admin"], team.workspace)
    with pytest.raises(AccessDenied):
        review.service.snapshot(context, review.document_id)
    shared(team, review)
    assert (
        review.service.activate(context, review.document_id, "parse_old", review.body)["revision"]
        == 1
    )


def test_other_workspace_cannot_enumerate_or_switch(team, review):
    context = team.store.context_for(team.principals["other_owner"], team.other)
    with pytest.raises(AccessDenied):
        review.service.snapshot(context, review.document_id)
    with pytest.raises(AccessDenied):
        review.service.activate(context, review.document_id, "parse_old", review.body)


def test_stale_revision_and_aba_are_rejected(team, review):
    service = review.service
    service.activate(team.owner, review.document_id, "parse_old", review.body)
    service.activate(
        team.owner,
        review.document_id,
        "parse_new",
        review.body.model_copy(update={"expected_revision": 1, "expected_parse_id": "parse_old"}),
    )
    with pytest.raises(IdentityConflict):
        service.activate(team.owner, review.document_id, "parse_old", review.body)
    assert service.snapshot(team.owner, review.document_id)["revision"] == 2


def test_concurrent_activations_have_one_winner(team, review):
    def activate(_):
        try:
            return review.service.activate(
                team.owner, review.document_id, "parse_old", review.body
            )["revision"]
        except IdentityConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(activate, range(2)), key=str) == [1, "conflict"]


def test_audit_failure_rolls_back_switch_metadata_and_review(team, review, monkeypatch):
    before = team.catalog.get_metadata(review.document_id)

    def fail(*args):
        raise OSError("injected audit failure")

    monkeypatch.setattr(team.store, "_audit", fail)
    with pytest.raises(OSError):
        review.service.activate(team.owner, review.document_id, "parse_old", review.body)
    assert team.catalog.active_parse_ids([review.document_id]) == ["parse_new"]
    assert team.catalog.get_document(review.document_id)["title"] == "new title"
    assert team.catalog.get_metadata(review.document_id) == before
    assert review.service.snapshot(team.owner, review.document_id)["history"] == []


@pytest.mark.parametrize(
    "corruption", ["source", "manifest", "chunks", "foreign_parse", "unregistered"]
)
def test_invalid_or_unindexed_candidate_cannot_be_published(team, review, corruption):
    directory = review.artifacts.parse_dir(review.document_id, "parse_old")
    if corruption == "source":
        path = directory / "canonical/document.json"
        data = json.loads(path.read_text())
        data["source_sha256"] = "b" * 64
        review.artifacts.write_json(path, data)
    elif corruption == "manifest":
        path = directory / "manifest.json"
        data = json.loads(path.read_text())
        data["document_id"] = "doc_other"
        review.artifacts.write_json(path, data)
    elif corruption == "chunks":
        path = directory / "chunks/chunks.json"
        data = json.loads(path.read_text())
        data[0]["document_id"] = "doc_other"
        review.artifacts.write_json(path, data)
    elif corruption == "foreign_parse":
        add_document(team, "doc_foreign")
        with team.store._transaction() as connection:
            connection.execute(
                "UPDATE parses SET document_id='doc_foreign' WHERE parse_id='parse_old'"
            )
    else:
        with team.store._transaction() as connection:
            connection.execute(
                "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE parse_id='parse_old')"
            )
            connection.execute("DELETE FROM chunks WHERE parse_id='parse_old'")
            connection.execute("DELETE FROM parses WHERE parse_id='parse_old'")
        versions = review.service.snapshot(team.owner, review.document_id)["versions"]
        assert next(v for v in versions if v["parse_id"] == "parse_old")["requires_indexing"]
    with pytest.raises(IdentityConflict):
        review.service.activate(team.owner, review.document_id, "parse_old", review.body)
    assert team.catalog.active_parse_ids([review.document_id]) == ["parse_new"]


def test_force_quality_gate_requires_explicit_confirmation(team, review):
    path = review.artifacts.parse_dir(review.document_id, "parse_old") / "manifest.json"
    data = json.loads(path.read_text())
    data["quality"]["decision"] = "fallback"
    review.artifacts.write_json(path, data)
    with pytest.raises(IdentityConflict):
        review.service.activate(team.owner, review.document_id, "parse_old", review.body)
    review.service.activate(
        team.owner, review.document_id, "parse_old", review.body.model_copy(update={"force": True})
    )
    assert review.service.snapshot(team.owner, review.document_id)["history"][0]["force"] == 1


def test_cached_context_is_revalidated_after_logout(team, review):
    team.store.revoke_session(team.principals["owner"].session_id)
    with pytest.raises(AccessDenied):
        review.service.activate(team.owner, review.document_id, "parse_old", review.body)


async def test_http_scope_actor_spoofing_and_legacy_block(team, review):
    app = create_app(team.settings)
    prefix = f"/api/workspaces/{team.workspace}/documents/{review.document_id}"
    headers = {"Authorization": f"Bearer {team.sessions['owner'][1]}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(prefix + "/parse-review")).status_code == 401
        response = await client.get(prefix + "/parse-review", headers=headers)
        assert (
            response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
        )
        body = review.body.model_dump()
        assert (
            await client.post(
                prefix + "/parses/parse_old/activate",
                json={**body, "actor": "spoof"},
                headers=headers,
            )
        ).status_code == 422
        assert (
            await client.post(prefix + "/parses/parse_old/activate", json=body, headers=headers)
        ).status_code == 200
        assert (
            await client.post(prefix + "/parses/parse_old/activate", json=body, headers=headers)
        ).status_code == 409
        assert (
            await client.get(f"/api/documents/{review.document_id}/parses", headers=headers)
        ).status_code == 501
