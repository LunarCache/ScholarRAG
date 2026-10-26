from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError
from test_workspace_identity import add_document
from test_workspace_identity import team as team  # noqa: PLC0414 - shared pytest fixture export

from scholarrag.api.main import create_app
from scholarrag.domain.access import AccessDenied, IdentityConflict, Role
from scholarrag.domain.library import (
    WorkspaceCollectionRequest,
    WorkspaceCollectionUpdate,
    WorkspaceStateRequest,
)
from scholarrag.storage.identity import IdentityStore
from scholarrag.storage.library import PersonalLibraryStore, PersonalPaperState
from scholarrag.storage.workspace_library import WorkspaceLibrary


@pytest.fixture
def library(team):
    for name in ("paper", "second", "foreign"):
        add_document(team, name)
    for name in ("paper", "second"):
        team.store.assign_document(name, team.workspace, team.owner.user_id)
    team.store.assign_document("foreign", team.other, team.users["other_owner"])
    return WorkspaceLibrary(team.store)


def member_context(team, name="member"):
    return team.store.context_for(team.principals[name], team.workspace)


def test_notes_and_collections_are_private_even_from_admins(team, library):
    member = member_context(team)
    saved = library.save_state(
        member, "paper", WorkspaceStateRequest(note="private note", tags=["method"], favorite=True)
    )
    assert saved.revision == 1
    collection = library.save_collection(
        member, WorkspaceCollectionRequest(name="private list", document_ids=["paper"])
    )
    for name in ("owner", "admin", "viewer"):
        context = member_context(team, name)
        assert library.get_state(context, "paper").note == ""
        assert library.list_collections(context) == []
        for action in (
            lambda context=context: library.get_collection(context, collection.collection_id),
            lambda context=context: library.save_collection(
                context,
                WorkspaceCollectionUpdate(name="overwrite", expected_revision=1),
                collection_id=collection.collection_id,
            ),
            lambda context=context: library.delete_collection(context, collection.collection_id),
        ):
            with pytest.raises(AccessDenied):
                action()
    assert library.get_state(member, "paper").note == "private note"


def test_scope_and_revoked_memberships_are_enforced_transactionally(team, library):
    context = member_context(team)
    for document_id in ("foreign", "unknown"):
        with pytest.raises(AccessDenied):
            library.save_state(context, document_id, WorkspaceStateRequest(note="test"))
        with pytest.raises(AccessDenied):
            library.get_state(context, document_id)
        with pytest.raises(AccessDenied):
            library.save_collection(
                context, WorkspaceCollectionRequest(name="test", document_ids=[document_id])
            )
    team.store.remove_member(team.owner, context.user_id)
    with pytest.raises(AccessDenied):
        library.list_collections(context)
    with pytest.raises(AccessDenied):
        library.save_state(context, "paper", WorkspaceStateRequest())


def test_stale_state_conflicts_and_deletion_does_not_reset_revision(team, library):
    state = library.get_state(team.owner, "paper")
    assert state.revision == 0 and state.updated_at is None
    library.save_state(team.owner, "paper", WorkspaceStateRequest(note="first"))
    with pytest.raises(IdentityConflict):
        library.save_state(team.owner, "paper", WorkspaceStateRequest(note="stale"))
    library.delete_state(team.owner, "paper")
    library.delete_state(team.owner, "paper")
    reset = library.get_state(team.owner, "paper")
    assert reset.note == "" and reset.revision == 2
    restored = library.save_state(
        team.owner, "paper", WorkspaceStateRequest(note="new", expected_revision=2)
    )
    assert restored.revision == 3
    with pytest.raises(IdentityConflict):
        library.save_state(
            team.owner, "paper", WorkspaceStateRequest(note="old tab", expected_revision=1)
        )


def test_concurrent_updates_have_one_winner(team, library):
    library.save_state(team.owner, "paper", WorkspaceStateRequest(note="initial"))

    def save(text):
        try:
            library.save_state(
                team.owner, "paper", WorkspaceStateRequest(note=text, expected_revision=1)
            )
            return True
        except IdentityConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, ["one", "two"]))
    assert sorted(outcomes) == [False, True]
    assert library.get_state(team.owner, "paper").revision == 2


def test_collection_updates_are_atomic_and_preserve_order(team, library):
    collection = library.save_collection(
        team.owner,
        WorkspaceCollectionRequest(name="ordered", document_ids=["second", "paper", "second"]),
    )
    assert collection.document_ids == ["second", "paper"]
    with pytest.raises(AccessDenied):
        library.save_collection(
            team.owner,
            WorkspaceCollectionUpdate(
                name="invalid", document_ids=["foreign"], expected_revision=1
            ),
            collection_id=collection.collection_id,
        )
    unchanged = library.get_collection(team.owner, collection.collection_id)
    assert unchanged.name == "ordered" and unchanged.revision == 1
    updated = library.save_collection(
        team.owner,
        WorkspaceCollectionUpdate(name="updated", document_ids=[], expected_revision=1),
        collection_id=collection.collection_id,
    )
    assert updated.revision == 2 and updated.document_ids == []
    assert updated.created_at == collection.created_at
    with pytest.raises(IdentityConflict):
        library.save_collection(
            team.owner,
            WorkspaceCollectionUpdate(name="stale", expected_revision=1),
            collection_id=collection.collection_id,
        )


def test_revoked_documents_are_hidden_but_owner_can_erase_private_data(team, library):
    library.save_state(team.owner, "paper", WorkspaceStateRequest(note="secret"))
    collection = library.save_collection(
        team.owner, WorkspaceCollectionRequest(name="mine", document_ids=["paper", "second"])
    )
    team.store.unassign_document("paper", team.workspace)
    with pytest.raises(AccessDenied):
        library.get_state(team.owner, "paper")
    view = library.get_collection(team.owner, collection.collection_id)
    assert view.document_ids == ["second"] and view.unavailable_document_count == 1
    library.delete_state(team.owner, "paper")
    library.delete_collection(team.owner, collection.collection_id)
    library.delete_collection(team.owner, collection.collection_id)
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        assert connection.execute("SELECT note FROM auth_personal_paper_state").fetchone()[0] == ""
        assert connection.execute("SELECT name FROM auth_collections").fetchone()[0] == ""
        assert (
            connection.execute("SELECT COUNT(*) FROM auth_collection_documents").fetchone()[0] == 0
        )


def test_viewer_cannot_write_but_can_erase_previous_own_data(team, library):
    member = member_context(team)
    library.save_state(member, "paper", WorkspaceStateRequest(note="old note"))
    collection = library.save_collection(member, WorkspaceCollectionRequest(name="old list"))
    team.store.set_member(team.owner, member.user_id, Role.VIEWER)
    with pytest.raises(AccessDenied):
        library.save_state(
            replace(member, role=Role.OWNER), "paper", WorkspaceStateRequest(expected_revision=1)
        )
    with pytest.raises(AccessDenied):
        library.save_collection(member, WorkspaceCollectionRequest(name="new"))
    library.delete_state(member, "paper")
    library.delete_collection(member, collection.collection_id)


def test_legacy_data_is_not_implicitly_migrated(team, library):
    legacy = PersonalLibraryStore(team.settings)
    legacy.save_state(
        PersonalPaperState(document_id="paper", note="legacy secret", updated_at="test")
    )
    legacy.save_collection("legacy list", ["paper"])
    assert library.get_state(team.owner, "paper").note == ""
    assert library.list_collections(team.owner) == []
    library.save_state(team.owner, "paper", WorkspaceStateRequest(note="workspace note"))
    assert legacy.get_state("paper").note == "legacy secret"


def test_inactive_documents_do_not_enter_personal_library_or_query_scope(team, library):
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        connection.execute("UPDATE parses SET active=0 WHERE document_id='paper'")
    assert "paper" not in team.store.document_ids(team.owner)
    with pytest.raises(AccessDenied):
        library.get_state(team.owner, "paper")


@pytest.mark.parametrize(
    "payload",
    [
        {"note": "x" * 20001},
        {"tags": [" "]},
        {"tags": ["x"] * 65},
        {"owner_id": "other"},
        {"expected_revision": -1},
    ],
)
def test_state_payload_is_bounded_and_cannot_supply_identity(payload):
    with pytest.raises(ValidationError):
        WorkspaceStateRequest(**payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"name": " "},
        {"name": "x" * 121},
        {"name": "ok", "document_ids": [""]},
        {"name": "ok", "document_ids": ["x"] * 1001},
        {"name": "ok", "workspace_id": "other"},
    ],
)
def test_collection_payload_is_bounded_and_cannot_supply_identity(payload):
    with pytest.raises(ValidationError):
        WorkspaceCollectionRequest(**payload)


async def test_workspace_personal_and_collection_http_contract(team, library):
    app = create_app(team.settings)
    prefix = f"/api/workspaces/{team.workspace}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {team.sessions['member'][1]}"},
    ) as client:
        state = await client.put(
            f"{prefix}/documents/paper/personal", json={"note": "member only", "favorite": True}
        )
        assert state.status_code == 200 and state.json()["revision"] == 1
        assert state.headers["cache-control"] == "private, no-store"
        assert (
            await client.put(f"{prefix}/documents/paper/personal", json={"note": "stale"})
        ).status_code == 409
        assert (
            await client.put(
                f"{prefix}/documents/paper/personal", json={"owner_id": team.owner.user_id}
            )
        ).status_code == 422
        created = await client.post(
            f"{prefix}/collections", json={"name": "reading", "document_ids": ["paper"]}
        )
        assert created.status_code == 201
        collection_id = created.json()["collection_id"]
        assert (await client.get(f"{prefix}/collections")).json()[0][
            "collection_id"
        ] == collection_id
        updated = await client.put(
            f"{prefix}/collections/{collection_id}",
            json={"name": "updated", "document_ids": ["second"], "expected_revision": 1},
        )
        assert updated.status_code == 200 and updated.json()["revision"] == 2
        assert (await client.get(f"{prefix}/collections?limit=0")).status_code == 422
        client.headers["Authorization"] = f"Bearer {team.sessions['admin'][1]}"
        assert (await client.get(f"{prefix}/documents/paper/personal")).json()["note"] == ""
        assert (await client.get(f"{prefix}/collections/{collection_id}")).status_code == 403
        assert (await client.delete(f"{prefix}/collections/{collection_id}")).status_code == 403
        client.headers["Authorization"] = f"Bearer {team.sessions['member'][1]}"
        assert (await client.delete(f"{prefix}/documents/paper/personal")).status_code == 204
        assert (await client.delete(f"{prefix}/collections/{collection_id}")).status_code == 204
        assert (await client.get(f"{prefix}/collections")).json() == []
        assert (await client.get("/api/collections")).status_code == 501


def test_v2_to_v3_migration_is_additive_and_idempotent(team, library):
    conversation_id = team.store.create_conversation(team.owner)
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        connection.execute("DROP TABLE auth_collection_documents")
        connection.execute("DROP TABLE auth_collections")
        connection.execute("DROP TABLE auth_personal_paper_state")
        connection.execute("DELETE FROM auth_schema_versions WHERE version=3")
    upgraded = IdentityStore(team.settings)
    IdentityStore(team.settings)
    assert upgraded.authenticate(team.sessions["owner"][1]).user_id == team.owner.user_id
    assert len(upgraded.list_members(team.owner)) == 4
    assert WorkspaceLibrary(upgraded).list_collections(team.owner) == []
    assert upgraded.owned_conversations(team.owner)[0]["conversation_id"] == conversation_id
    assert upgraded.document_ids(team.owner) == ["paper", "second"]


def test_database_failure_rolls_back_collection_name_members_revision_and_audit(team, library):
    collection = library.save_collection(
        team.owner, WorkspaceCollectionRequest(name="original", document_ids=["paper"])
    )
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_collection_insert BEFORE INSERT ON auth_collection_documents
               WHEN NEW.document_id='second' BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic write failure"):
        library.save_collection(
            team.owner,
            WorkspaceCollectionUpdate(
                name="must roll back", document_ids=["second"], expected_revision=1
            ),
            collection_id=collection.collection_id,
        )
    restored = library.get_collection(team.owner, collection.collection_id)
    assert restored.name == "original" and restored.document_ids == ["paper"]
    assert restored.revision == 1
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM auth_audit WHERE action='collection.saved'"
            ).fetchone()[0]
            == 1
        )
