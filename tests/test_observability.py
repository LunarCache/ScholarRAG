from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import contextmanager

import httpx
import pytest

from scholarrag.config import Settings
from scholarrag.observability import Telemetry, observe
from scholarrag.providers.siliconflow import SiliconFlowClient
from scholarrag.rag.runs import PipelineTracer


@pytest.fixture
def exported(monkeypatch: pytest.MonkeyPatch):
    """Use the real SDK/OTel context and an in-memory exporter; no Cloud traffic."""
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    client = langfuse.Langfuse(
        public_key=f"pk-test-{uuid.uuid4().hex}",
        secret_key="sk-test",
        span_exporter=exporter,
        tracer_provider=TracerProvider(),
    )
    monkeypatch.setattr(langfuse, "Langfuse", lambda **kwargs: client)
    yield client, exporter
    client.shutdown()


def settings(**kwargs) -> Settings:
    return Settings(
        _env_file=None,
        langfuse_enabled=True,
        LANGFUSE_PUBLIC_KEY="pk-test",
        LANGFUSE_SECRET_KEY="sk-test",
        **kwargs,
    )


def test_disabled_needs_no_credentials_or_sdk() -> None:
    telemetry = Telemetry(Settings(_env_file=None, langfuse_enabled=False))
    assert telemetry.client is None
    with telemetry.trace("disabled", trace_id=uuid.uuid4().hex), observe("child"):
        pass


def test_missing_credentials_explains_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="LANGFUSE_PUBLIC_KEY"):
        Telemetry(Settings(_env_file=None, langfuse_enabled=True))


async def test_real_sdk_parallel_stages_and_stream_usage(exported, tmp_path) -> None:
    client, exporter = exported
    telemetry = Telemetry(settings())
    trace_id = uuid.uuid4().hex

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"private answer"}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"x-siliconcloud-trace-id": "provider-test"},
        )

    async def branch(name: str) -> None:
        async with PipelineTracer().stage(name, name, input={"query": "private paper"}):
            await asyncio.sleep(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = SiliconFlowClient(
            Settings(_env_file=None, data_dir=tmp_path, SILICONFLOW_API_KEY="test"), client=http
        )
        with telemetry.trace(
            "root", trace_id=trace_id, session_id="conversation-test", input="private question"
        ):
            await asyncio.gather(branch("dense"), branch("lexical"))
            async with provider.chat_stream([{"role": "user", "content": "private prompt"}]) as s:
                chunks = [chunk async for chunk in s.chunks()]
            assert chunks[0]["choices"][0]["delta"]["content"] == "private answer"
    client.flush()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"root", "dense", "lexical", "siliconflow.chat.stream"}
    root = spans["root"]
    for span in spans.values():
        assert f"{span.context.trace_id:032x}" == trace_id
        assert span.attributes["session.id"] == "conversation-test"
        if span is not root:
            assert span.parent.span_id == root.context.span_id
    generation = spans["siliconflow.chat.stream"]
    assert generation.attributes["langfuse.observation.type"] == "generation"
    usage = json.loads(generation.attributes["langfuse.observation.usage_details"])
    assert usage == {"input": 7, "output": 3}
    assert "private" not in str([dict(span.attributes) for span in spans.values()])


async def test_concurrent_traces_keep_their_own_parent(exported) -> None:
    client, exporter = exported
    telemetry = Telemetry(settings())
    ids = {"one": uuid.uuid4().hex, "two": uuid.uuid4().hex}

    async def run(name: str) -> None:
        with telemetry.trace(name, trace_id=ids[name]):
            await asyncio.sleep(0)
            with observe(f"{name}.child"):
                await asyncio.sleep(0)

    await asyncio.gather(run("one"), run("two"))
    client.flush()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    for name, trace_id in ids.items():
        child = spans[f"{name}.child"]
        assert f"{child.context.trace_id:032x}" == trace_id
        assert child.parent.span_id == spans[name].context.span_id


def test_opt_in_omits_media_and_reasoning(exported) -> None:
    client, exporter = exported
    telemetry = Telemetry(settings(langfuse_capture_content=True))
    with (
        telemetry.trace("root", trace_id=uuid.uuid4().hex, input="visible question"),
        observe(
            "child",
            input={
                "image_url": "data:image/png;base64,secret",
                "text": "ok",
                "reasoning_content": "hidden reasoning",
            },
        ),
    ):
        pass
    client.flush()
    attrs = str([dict(span.attributes) for span in exporter.get_finished_spans()])
    assert "visible question" in attrs
    assert "base64" not in attrs
    assert "hidden reasoning" not in attrs


def test_telemetry_failure_does_not_retry_or_swallow_business_error(exported) -> None:
    telemetry = Telemetry(settings())

    class BrokenClient:
        @contextmanager
        def start_as_current_observation(self, **kwargs):
            raise RuntimeError("export unavailable")
            yield  # pragma: no cover

    telemetry.client = BrokenClient()
    calls = []
    with (
        pytest.raises(LookupError, match="business failure"),
        telemetry.trace("root", trace_id=uuid.uuid4().hex),
    ):
        calls.append(1)
        raise LookupError("business failure")
    assert calls == [1]


async def test_cancel_preserves_cancellation_and_redacts_errors(exported) -> None:
    client, exporter = exported
    telemetry = Telemetry(settings())
    with (
        pytest.raises(asyncio.CancelledError),
        telemetry.trace("cancelled", trace_id=uuid.uuid4().hex),
    ):
        raise asyncio.CancelledError("private error text")
    client.flush()
    span = exporter.get_finished_spans()[0]
    assert span.attributes["langfuse.observation.level"] == "ERROR"
    assert "private error text" not in str(dict(span.attributes))


async def test_persisted_run_and_langfuse_share_trace_id(exported, tmp_path, monkeypatch) -> None:
    from scholarrag.api.main import Runtime, _run_query
    from scholarrag.domain.models import Answer
    from scholarrag.rag.service import RAGService

    client, exporter = exported
    runtime = Runtime(settings(data_dir=tmp_path))

    async def answer(self, question, **kwargs):
        async with PipelineTracer(kwargs["events"]).stage("test.retrieve", "test"):
            return Answer(answer="private response", citations=[], trace_id=kwargs["trace_id"])

    monkeypatch.setattr(RAGService, "answer", answer)
    run = await runtime.query_runs.create(
        question="private question", document_ids=[], top_k=8, agentic=False
    )
    await _run_query(runtime, run.run_id)
    completed = runtime.query_runs.get(run.run_id)
    assert completed.status == "completed"
    assert completed.answer.answer == "private response"
    assert runtime.query_runs.list_events(run.run_id)[-1].type == "run.completed"
    client.flush()
    root = next(s for s in exporter.get_finished_spans() if s.name == "scholarrag.query")
    assert f"{root.context.trace_id:032x}" == run.trace_id
    assert root.attributes["langfuse.observation.status_message"] == "completed"


def test_smoke_exports_synthetic_trace_without_llm(exported, monkeypatch) -> None:
    from typer.testing import CliRunner

    from scholarrag import cli

    client, exporter = exported
    monkeypatch.setattr(cli, "get_settings", lambda: settings())
    monkeypatch.setattr(client, "auth_check", lambda: True)
    result = CliRunner().invoke(cli.app, ["langfuse-smoke"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["authenticated"] is True
    assert payload["synthetic"] is True
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} == {
        "scholarrag.smoke",
        "example.retrieval",
        "example.generation",
    }


def test_verified_user_and_workspace_attributes_propagate_to_children(exported):
    client, exporter = exported
    telemetry = Telemetry(settings())
    with (
        telemetry.trace(
            "workspace",
            trace_id=uuid.uuid4().hex,
            user_id="usr-verified",
            metadata={"workspace_id": "ws-verified"},
        ),
        observe("model-call", as_type="generation", model="synthetic"),
    ):
        pass
    client.flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        assert span.attributes["user.id"] == "usr-verified"
        assert "ws-verified" in str(dict(span.attributes))
