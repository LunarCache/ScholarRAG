from __future__ import annotations

import asyncio

import httpx
import pytest
from test_workspace_identity import add_document
from test_workspace_identity import team as team  # noqa: PLC0414 - shared pytest fixture export

from scholarrag.api.main import Runtime, create_app
from scholarrag.api.schemas import QueryRequest
from scholarrag.authorization import AuthorizationGuard, bind_authorization, current_guard
from scholarrag.domain.access import AccessDenied
from scholarrag.domain.models import Answer
from scholarrag.rag.runs import QueryRunStore
from scholarrag.rag.service import RAGService
from scholarrag.retrieval.service import RetrievalService


@pytest.fixture
async def application(team, monkeypatch):
    async def answer(self, question, **kwargs):
        guard = current_guard(self.settings)
        await asyncio.sleep(0)
        return Answer(
            answer=f"{guard.context.user_id}: {question}", citations=[], trace_id=kwargs["trace_id"]
        )

    monkeypatch.setattr(RAGService, "answer", answer)
    app = create_app(team.settings.model_copy(update={"authorization_poll_seconds": 0.1}))
    yield app
    tasks = list(app.state.runtime.tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def headers(team, name="owner"):
    return {"Authorization": f"Bearer {team.sessions[name][1]}"}


async def complete(client, prefix, run_id):
    for _ in range(100):
        result = await client.get(f"{prefix}/query-runs/{run_id}")
        assert result.status_code == 200, result.text
        if result.json()["status"] in {"completed", "failed", "cancelled"}:
            return result.json()
        await asyncio.sleep(0.01)
    pytest.fail("Query did not finish")


async def test_private_run_history_sse_export_and_namespace(application, team):
    prefix = f"/api/workspaces/{team.workspace}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
        headers=headers(team),
    ) as client:
        created = await client.post(
            f"{prefix}/query-runs",
            json={"question": "private question", "document_ids": [], "agentic": False},
        )
        assert created.status_code == 202, created.text
        run = created.json()
        assert run["workspace_id"] == team.workspace and run["owner_id"] == team.owner.user_id
        assert "artifact_dir" not in run and "actor_session_id" not in run
        completed = await complete(client, prefix, run["run_id"])
        assert completed["status"] == "completed"
        assert "private question" in completed["answer"]["answer"]
        assert QueryRunStore(team.settings).list() == []
        events = (await client.get(f"{prefix}/query-runs/{run['run_id']}/event-log")).json()
        assert events[-1]["type"] == "run.completed"
        stream = await client.get(
            f"{prefix}/query-runs/{run['run_id']}/events", headers={"Last-Event-ID": "1"}
        )
        assert "id: 1\n" not in stream.text and "run.completed" in stream.text
        assert stream.headers["cache-control"] == "private, no-store"
        assert (
            await client.get(
                f"{prefix}/query-runs/{run['run_id']}/events", headers={"Last-Event-ID": "-1"}
            )
        ).status_code == 422
        assert (await client.get(f"{prefix}/query-runs/{run['run_id']}/export")).status_code == 200
        for name in ("admin", "member", "other_owner"):
            client.headers.update(headers(team, name))
            for suffix, method in (
                ("", "GET"),
                ("/event-log", "GET"),
                ("/events", "GET"),
                ("/export", "GET"),
                ("/cancel", "POST"),
            ):
                assert (
                    await client.request(method, f"{prefix}/query-runs/{run['run_id']}{suffix}")
                ).status_code == 403
            assert (
                await client.get(f"{prefix}/conversations/{run['conversation_id']}")
            ).status_code == 403


async def test_parent_context_requires_same_private_conversation_and_scope(application, team):
    prefix = f"/api/workspaces/{team.workspace}"
    add_document(team, "paper")
    team.store.assign_document("paper", team.workspace, team.owner.user_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
        headers=headers(team),
    ) as client:
        parent = (
            await client.post(f"{prefix}/query-runs", json={"question": "parent", "agentic": False})
        ).json()
        await complete(client, prefix, parent["run_id"])
        body = {
            "question": "follow up",
            "agentic": False,
            "parent_run_id": parent["run_id"],
            "conversation_id": parent["conversation_id"],
        }
        child = await client.post(f"{prefix}/query-runs", json=body)
        assert child.status_code == 202
        result = await complete(client, prefix, child.json()["run_id"])
        assert "上轮问题：parent" in result["answer"]["answer"]
        assert (
            await client.post(f"{prefix}/query-runs", json={**body, "conversation_id": None})
        ).status_code == 403
        assert (
            await client.post(f"{prefix}/query-runs", json={**body, "document_ids": []})
        ).status_code == 409
        client.headers.update(headers(team, "member"))
        assert (await client.post(f"{prefix}/query-runs", json=body)).status_code == 403


async def test_out_of_workspace_documents_are_rejected_before_submission(application, team):
    add_document(team, "foreign")
    team.store.assign_document("foreign", team.other, team.users["other_owner"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
        headers=headers(team),
    ) as client:
        result = await client.post(
            f"/api/workspaces/{team.workspace}/query-runs",
            json={"question": "test", "document_ids": ["foreign"]},
        )
        assert result.status_code == 403
    assert team.store.owned_query_ids(team.owner) == []


async def test_sse_stops_on_revocation_before_next_private_event(application, team):
    runtime = application.state.runtime
    context = team.store.context_for(team.principals["member"], team.workspace)
    run = await runtime.workspace_queries.create(
        context, QueryRequest(question="private", document_ids=[], agentic=False)
    )

    async def connected():
        return False

    stream = runtime.workspace_queries.events(
        context, run.run_id, after_seq=0, disconnected=connected
    )
    assert "run.created" in await anext(stream)
    team.store.remove_member(team.owner, context.user_id)
    assert "access.revoked" in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_watcher_cancels_inflight_work_after_session_revocation(
    application, team, monkeypatch
):
    runtime = application.state.runtime
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RAGService, "answer", blocked)
    context = team.store.context_for(team.principals["member"], team.workspace)
    run = await runtime.workspace_queries.create(
        context, QueryRequest(question="wait", document_ids=[], agentic=False)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task = runtime.workspace_queries.active[run.run_id][1]
    team.store.revoke_session(context.session_id)
    await asyncio.wait_for(task, timeout=1)
    final = runtime.workspace_queries.store(team.workspace).get(run.run_id)
    assert final.status == "cancelled" and final.answer is None
    assert final.error == "Authorization revoked"


async def test_authorization_is_rechecked_before_answer_persistence(application, team, monkeypatch):
    runtime = application.state.runtime
    add_document(team, "paper")
    team.store.assign_document("paper", team.workspace, team.owner.user_id)

    async def revoked(self, question, **kwargs):
        team.store.unassign_document("paper", team.workspace)
        return Answer(answer="must not persist", citations=[], trace_id=kwargs["trace_id"])

    monkeypatch.setattr(RAGService, "answer", revoked)
    run = await runtime.workspace_queries.create(
        team.owner, QueryRequest(question="test", agentic=False)
    )
    await runtime.workspace_queries.active[run.run_id][1]
    final = runtime.workspace_queries.store(team.workspace).get(run.run_id)
    assert final.status == "failed" and final.answer is None
    with pytest.raises(AccessDenied):
        runtime.workspace_queries.read(team.owner, run.run_id)


async def test_active_task_limits_and_queued_cancellation(application, team, monkeypatch):
    runtime = application.state.runtime
    runtime.workspace_queries.settings = runtime.settings.model_copy(
        update={"workspace_query_max_per_user": 1}
    )

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(RAGService, "answer", blocked)
    run = await runtime.workspace_queries.create(
        team.owner, QueryRequest(question="test", document_ids=[], agentic=False)
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await runtime.workspace_queries.create(team.owner, QueryRequest(question="excess"))
    assert error.value.status_code == 429
    final = await runtime.workspace_queries.cancel(team.owner, run.run_id)
    assert final.status == "cancelled"
    assert len(team.store.owned_query_ids(team.owner)) == 1


async def test_namespaced_recovery_preserves_legacy_runs(application, team):
    runtime = application.state.runtime

    async def pending(*args):
        await asyncio.Event().wait()

    runtime.workspace_queries.execute = pending
    run = await runtime.workspace_queries.create(
        team.owner, QueryRequest(question="interrupted", document_ids=[])
    )
    task = runtime.workspace_queries.active[run.run_id][1]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    legacy = await runtime.query_runs.create(question="legacy", document_ids=[], top_k=8)
    restarted = Runtime(team.settings)
    await restarted.workspace_queries.recover()
    assert restarted.workspace_queries.store(team.workspace).get(run.run_id).status == "failed"
    assert restarted.query_runs.get(legacy.run_id).status == "queued"


async def test_session_retrieval_requires_guard_and_rejects_scope_expansion(team):
    with pytest.raises(AccessDenied):
        await RetrievalService(team.settings).retrieve("test", document_ids=[])
    guard = AuthorizationGuard(team.store, team.owner, ())
    with bind_authorization(guard), pytest.raises(PermissionError):
        await RetrievalService(team.settings).retrieve("test", document_ids=["foreign"])


async def test_concurrent_users_do_not_share_execution_context(application, team):
    prefix = f"/api/workspaces/{team.workspace}"

    async def ask(name):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
            headers=headers(team, name),
        ) as client:
            created = (
                await client.post(
                    f"{prefix}/query-runs",
                    json={"question": name, "agentic": False, "document_ids": []},
                )
            ).json()
            result = await complete(client, prefix, created["run_id"])
            assert result["answer"]["answer"] == f"{team.users[name]}: {name}"
            assert len((await client.get(f"{prefix}/query-runs")).json()) == 1

    await asyncio.gather(ask("owner"), ask("member"))


def test_v1_schema_upgrade_preserves_users_and_memberships(team):
    import sqlite3

    with sqlite3.connect(team.settings.sqlite_path) as connection:
        connection.execute("DROP TABLE auth_query_runs")
        connection.execute("DROP TABLE auth_conversations")
        connection.execute("DROP TABLE auth_collection_documents")
        connection.execute("DROP TABLE auth_collections")
        connection.execute("DROP TABLE auth_personal_paper_state")
        connection.execute("DELETE FROM auth_schema_versions WHERE version>=2")
    from scholarrag.storage.identity import IdentityStore

    upgraded = IdentityStore(team.settings)
    assert upgraded.authenticate(team.sessions["owner"][1]).user_id == team.owner.user_id
    assert len(upgraded.list_members(team.owner)) == 4


async def test_provider_rechecks_authorization_after_waiting_for_capacity(team):
    from scholarrag.providers.siliconflow import SiliconFlowClient

    add_document(team, "paper")
    team.store.assign_document("paper", team.workspace, team.owner.user_id)
    settings = team.settings.model_copy(update={"provider_max_concurrency": 1})
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        first = SiliconFlowClient(settings, client=http)
        second = SiliconFlowClient(settings, client=http)
        with bind_authorization(AuthorizationGuard(team.store, team.owner, ("paper",))):
            async with first._request_slot():
                pending = asyncio.create_task(second.chat([]))
                await asyncio.sleep(0.01)
                team.store.unassign_document("paper", team.workspace)
            with pytest.raises(AccessDenied):
                await pending
        assert calls == []
        with bind_authorization(AuthorizationGuard(team.store, team.owner, ())):
            await second.chat([])
        assert len(calls) == 1


async def test_workspace_tool_agent_inherits_scope_and_returns_owned_citation(
    application, team, monkeypatch
):
    from test_tool_agent import make_chunk

    from scholarrag.domain.models import CanonicalDocument, RetrievedChunk
    from scholarrag.rag.tool_agent import ToolAgentService

    chunk = make_chunk("guarded-evidence")
    document = CanonicalDocument(
        document_id=chunk.document_id,
        parse_id=chunk.parse_id,
        source_sha256="a" * 64,
        source_filename="synthetic.pdf",
        provider="test",
        provider_model="test",
    )
    team.catalog.register(
        document,
        [chunk],
        source_path=team.settings.data_dir / "synthetic.pdf",
        artifact_dir=team.settings.data_dir / "synthetic-artifact",
    )
    team.store.assign_document(chunk.document_id, team.workspace, team.owner.user_id)
    calls = []

    async def retrieve(self, query, **kwargs):
        assert current_guard(self.settings).context.user_id == team.owner.user_id
        assert kwargs["document_ids"] == [chunk.document_id]
        return [RetrievedChunk(chunk=chunk, score=1)], {}

    async def chat(self, provider, messages, tools, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return (
                "",
                [{"id": "lookup", "name": "retrieve", "arguments": '{"query":"方法"}'}],
                {},
                None,
            )
        return "该方法使用混合检索与重排 [C1]。", [], {}, None

    monkeypatch.setattr(RetrievalService, "retrieve", retrieve)
    monkeypatch.setattr(ToolAgentService, "_chat_with_tools", chat)
    prefix = f"/api/workspaces/{team.workspace}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
        headers=headers(team),
    ) as client:
        created = await client.post(
            f"{prefix}/query-runs",
            json={"question": "论文方法是什么？", "agentic": True, "tool_mode": True},
        )
        assert created.status_code == 202
        final = await complete(client, prefix, created.json()["run_id"])
    assert final["status"] == "completed"
    assert final["answer"]["retrieval_debug"]["agent_stop_reason"] == "tool_loop_complete"
    assert final["answer"]["citations"][0]["document_id"] == chunk.document_id
    assert len(calls) == 2
