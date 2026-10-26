from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from scholarrag.api.main import create_app
from scholarrag.config import Settings
from scholarrag.domain.access import AccessDenied, IdentityConflict, Principal, Role
from scholarrag.domain.models import CanonicalDocument
from scholarrag.rag.tools import EvidenceRegistry, ToolRuntime, build_tool_specs
from scholarrag.storage.catalog import Catalog
from scholarrag.storage.identity import IdentityStore


@pytest.fixture
def team(tmp_path):
    settings = Settings(data_dir=tmp_path, auth_mode="session")
    catalog = Catalog(settings)
    store = IdentityStore(settings)
    users = {
        name: store.create_user(name, issuer="test", subject=name)
        for name in ("owner", "other_owner", "admin", "member", "viewer")
    }
    sessions = {name: store.issue_session(user_id) for name, user_id in users.items()}
    principals = {name: store.authenticate(value[1]) for name, value in sessions.items()}
    workspace = store.create_workspace(users["owner"], "Team A")
    other = store.create_workspace(users["other_owner"], "Team B")
    context = store.context_for(principals["owner"], workspace)
    for name in ("admin", "member", "viewer"):
        store.set_member(context, users[name], Role(name))
    return SimpleNamespace(
        settings=settings,
        catalog=catalog,
        store=store,
        users=users,
        sessions=sessions,
        principals=principals,
        workspace=workspace,
        other=other,
        owner=context,
    )


def add_document(team, name):
    path = team.settings.data_dir / f"{name}.pdf"
    path.write_bytes(b"synthetic PDF test bytes")
    document = CanonicalDocument(
        document_id=name,
        parse_id=f"parse-{name}",
        source_sha256="a" * 64,
        source_filename=f"{name}.pdf",
        provider="test",
        provider_model="test",
    )
    team.catalog.register(document, [], source_path=path, artifact_dir=path.parent / name)


def test_schema_is_idempotent_and_sessions_are_hashed(team):
    IdentityStore(team.settings)
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_schema_versions").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0] == 5
        values = str(connection.execute("SELECT * FROM auth_sessions").fetchall())
        audit = str(connection.execute("SELECT * FROM auth_audit").fetchall())
    for session_id, token in team.sessions.values():
        assert token not in values and token not in audit
        assert team.store.authenticate(token).session_id == session_id
    with pytest.raises(IdentityConflict):
        team.store.create_user("renamed", issuer="test", subject="owner")


@pytest.mark.parametrize(
    ("name", "can_upload", "can_manage"),
    [
        ("viewer", False, False),
        ("member", True, False),
        ("admin", True, True),
        ("owner", True, True),
    ],
)
def test_role_matrix_and_untrusted_role_snapshot(team, name, can_upload, can_manage):
    context = team.store.context_for(team.principals[name], team.workspace)
    assert team.store.authorize(context, "read")
    for action, allowed in (("upload", can_upload), ("manage_members", can_manage)):
        if allowed:
            team.store.authorize(context, action)
        else:
            with pytest.raises(AccessDenied):
                team.store.authorize(replace(context, role=Role.OWNER), action)
    with pytest.raises(AccessDenied):
        team.store.authorize(context, "unknown-action")


def test_membership_revocation_and_session_revocation_recheck_old_context(team):
    member = team.store.context_for(team.principals["member"], team.workspace)
    team.store.remove_member(team.owner, member.user_id)
    with pytest.raises(AccessDenied):
        team.store.authorize(member, "read")
    team.store.set_member(team.owner, member.user_id, Role.MEMBER)
    team.store.revoke_session(member.session_id)
    with pytest.raises(AccessDenied):
        team.store.authorize(member, "read")
    with pytest.raises(AccessDenied):
        team.store.authenticate(team.sessions["member"][1])


@pytest.mark.parametrize("change", ["expired", "user-disabled", "workspace-disabled"])
def test_disabled_or_expired_authority_is_denied(team, change):
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        if change == "expired":
            connection.execute("UPDATE auth_sessions SET expires_at=0")
        elif change == "user-disabled":
            connection.execute("UPDATE auth_users SET active=0")
        else:
            connection.execute("UPDATE auth_workspaces SET active=0")
    with pytest.raises(AccessDenied):
        team.store.authorize(team.owner, "read")


def test_workspace_enumeration_and_forged_principal_do_not_cross_boundaries(team):
    assert [w["workspace_id"] for w in team.store.list_workspaces(team.principals["owner"])] == [
        team.workspace
    ]
    with pytest.raises(AccessDenied):
        team.store.context_for(team.principals["owner"], team.other)
    with pytest.raises(AccessDenied):
        team.store.context_for(
            Principal(team.users["other_owner"], team.owner.session_id), team.other
        )


def test_owner_invariant_and_admin_cannot_escalate(team):
    admin = team.store.context_for(team.principals["admin"], team.workspace)
    for target, role in (("member", Role.ADMIN), ("admin", Role.MEMBER)):
        with pytest.raises(AccessDenied):
            team.store.set_member(admin, team.users[target], role)
    with pytest.raises(IdentityConflict):
        team.store.remove_member(team.owner, team.owner.user_id)
    with pytest.raises(IdentityConflict):
        team.store.set_member(team.owner, team.owner.user_id, Role.MEMBER)
    with pytest.raises(IdentityConflict):
        team.store.transfer_owner(team.owner, team.users["other_owner"])
    team.store.transfer_owner(team.owner, team.users["member"])
    with pytest.raises(AccessDenied):
        team.store.transfer_owner(team.owner, team.users["admin"])
    roles = {row["user_id"]: row["role"] for row in team.store.list_members(team.owner)}
    assert roles[team.users["member"]] == "owner" and roles[team.users["owner"]] == "admin"


def test_concurrent_owner_transfers_have_one_winner(team):
    def transfer(target):
        try:
            team.store.transfer_owner(team.owner, team.users[target])
            return True
        except AccessDenied:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(transfer, ["member", "admin"]))
    assert sorted(outcomes) == [False, True]
    with sqlite3.connect(team.settings.sqlite_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM auth_memberships WHERE workspace_id=? AND role='owner' AND active=1",
                (team.workspace,),
            ).fetchone()[0]
            == 1
        )


async def test_tool_context_uses_grants_and_rechecks_revocation(team):
    add_document(team, "paper")
    context = team.store.context_for(team.principals["member"], team.workspace)
    assert team.store.document_ids(context) == []
    team.store.assign_document("paper", team.workspace, team.users["owner"])
    with pytest.raises(IdentityConflict):
        team.store.assign_document("paper", team.other, team.users["other_owner"])
    with pytest.raises(AccessDenied):
        ToolRuntime(team.settings, EvidenceRegistry())
    runtime = ToolRuntime(
        team.settings, EvidenceRegistry(), access_context=context, identities=team.store
    )
    listed = await build_tool_specs()[0].handler(runtime, {})
    assert [p["document_id"] for p in listed["papers"]] == ["paper"]
    team.store.remove_member(team.owner, context.user_id)
    with pytest.raises(AccessDenied):
        await build_tool_specs()[0].handler(runtime, {})


async def test_api_auth_roles_document_filter_and_legacy_fail_closed(team):
    add_document(team, "visible")
    add_document(team, "unassigned")
    team.store.assign_document("visible", team.workspace, team.users["owner"])
    app = create_app(team.settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (
            await c.get("/api/me", headers={"X-User-Id": team.users["owner"]})
        ).status_code == 401
        c.headers["Authorization"] = f"Bearer {team.sessions['viewer'][1]}"
        assert (await c.get("/api/me")).json()["user_id"] == team.users["viewer"]
        denied = await c.put(
            f"/api/workspaces/{team.workspace}/members/{team.users['viewer']}",
            json={"role": "owner"},
        )
        assert denied.status_code == 403
        assert (await c.get(f"/api/workspaces/{team.other}/members")).status_code == 403
        docs = (await c.get(f"/api/workspaces/{team.workspace}/documents")).json()
        assert [d["document_id"] for d in docs] == ["visible"]
        assert "source_path" not in docs[0] and "artifact_dir" not in docs[0]
        assert (
            await c.get(f"/api/workspaces/{team.workspace}/documents/unassigned/pdf")
        ).status_code == 403
        assert (
            await c.get(f"/api/workspaces/{team.workspace}/documents/visible/pdf")
        ).status_code == 200
        for path in (
            "/api/documents",
            "/api/query-runs",
            "/api/dashboard",
            "/api/reviews/summary",
            "/api/conversations",
        ):
            assert (await c.get(path)).status_code == 501
        team.store.revoke_session(team.sessions["viewer"][0])
        assert (await c.get("/api/me")).status_code == 401
        assert (await c.get("/api/health")).status_code == 200


def test_cli_provisioning_and_revocation(team, monkeypatch):
    import json

    from scholarrag import cli, cli_auth

    monkeypatch.setattr(cli_auth, "get_settings", lambda: team.settings)
    runner = CliRunner()
    issued = runner.invoke(cli.app, ["auth", "issue-session", team.users["owner"]])
    assert issued.exit_code == 0, issued.output
    payload = json.loads(issued.stdout)
    assert team.store.authenticate(payload["token"])
    revoked = runner.invoke(cli.app, ["auth", "revoke-session", payload["session_id"]])
    assert revoked.exit_code == 0
    with pytest.raises(AccessDenied):
        team.store.authenticate(payload["token"])


async def test_revocation_during_retrieval_discards_results(team, monkeypatch):
    context = team.store.context_for(team.principals["member"], team.workspace)
    add_document(team, "paper")
    team.store.assign_document("paper", team.workspace, team.owner.user_id)
    runtime = ToolRuntime(
        team.settings, EvidenceRegistry(), access_context=context, identities=team.store
    )

    async def retrieve(*args, **kwargs):
        team.store.remove_member(team.owner, context.user_id)
        return [], {}

    monkeypatch.setattr(runtime.retrieval, "retrieve", retrieve)
    retrieve_tool = next(tool for tool in build_tool_specs() if tool.name == "retrieve")
    with pytest.raises(AccessDenied):
        await retrieve_tool.handler(runtime, {"query": "test"})


async def test_auth_attempts_are_rate_limited_before_lookup(team):
    app = create_app(team.settings.model_copy(update={"api_requests_per_minute": 10}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        codes = [
            (await c.get("/api/me", headers={"Authorization": "Bearer invalid"})).status_code
            for _ in range(11)
        ]
    assert codes == [401] * 10 + [429]


async def test_local_mode_stays_compatible_and_does_not_expose_identity_api(team):
    app = create_app(team.settings.model_copy(update={"auth_mode": "local"}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.get("/api/documents")).status_code == 200
        assert (await c.get("/api/me")).status_code == 404


def test_tool_scope_snapshot_does_not_expand_with_new_grants(team):
    runtime = ToolRuntime(
        team.settings, EvidenceRegistry(), access_context=team.owner, identities=team.store
    )
    add_document(team, "later")
    team.store.assign_document("later", team.workspace, team.owner.user_id)
    runtime.refresh_scope()
    assert runtime.scope.resolve() == []


async def test_logout_revokes_only_current_session_and_private_responses_are_not_cached(team):
    second_id, second_token = team.store.issue_session(team.owner.user_id)
    app = create_app(team.settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.headers["Authorization"] = f"Bearer {team.sessions['owner'][1]}"
        profile = await c.get("/api/me")
        assert profile.headers["cache-control"] == "private, no-store"
        assert (await c.delete("/api/me/session")).status_code == 204
        assert (await c.get("/api/me")).status_code == 401
    assert team.store.authenticate(second_token).session_id == second_id


def test_assignment_rollback_does_not_delete_original_document(team):
    add_document(team, "paper")
    team.store.assign_document("paper", team.workspace, team.owner.user_id)
    team.store.unassign_document("paper", team.workspace)
    assert team.store.document_ids(team.owner) == []
    assert team.catalog.get_document("paper")
    assert (team.settings.data_dir / "paper.pdf").exists()
