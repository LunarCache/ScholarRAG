"""Unit tests for the tool-calling agentic RAG loop.

Provider round-trips are faked: the chat turns are scripted and retrieval
returns synthetic chunks, so the tests exercise the loop, tool execution,
evidence registry numbering, citation extraction and fallback behaviour
without any network calls.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scholarrag.config import Settings
from scholarrag.domain.models import (
    Answer,
    Chunk,
    RetrievedChunk,
    SourcePointer,
)
from scholarrag.rag.tool_agent import ToolAgentService
from scholarrag.rag.tools import (
    EvidenceRegistry,
    build_tool_specs,
)


def make_chunk(chunk_id: str, text: str = "核心方法使用混合检索与重排。") -> Chunk:
    return Chunk(
        id=chunk_id,
        document_id="doc-1",
        parse_id="parse-1",
        text=text,
        contextual_text=f"测试论文\n\n{text}",
        block_ids=[f"block-{chunk_id}"],
        page_numbers=[3],
        section_path=["方法"],
        token_estimate=10,
        sources=[
            SourcePointer(
                document_id="doc-1",
                parse_id="parse-1",
                provider="test",
                page_number=3,
                raw_artifact="raw.json",
            )
        ],
    )


def test_evidence_registry_assigns_stable_indices() -> None:
    registry = EvidenceRegistry()
    first = RetrievedChunk(chunk=make_chunk("chk-a"), score=0.9)
    second = RetrievedChunk(chunk=make_chunk("chk-b"), score=0.8)
    assert registry.register(first) == 1
    assert registry.register(second) == 2
    # Re-registering the same chunk keeps its original number.
    assert registry.register(first) == 1
    assert registry.get("chk-b") is second
    assert len(registry.pool) == 2


def test_tool_specs_are_openai_compatible() -> None:
    specs = build_tool_specs()
    names = [spec.name for spec in specs]
    assert names == ["list_papers", "get_paper", "retrieve", "read_chunk"]
    for spec in specs:
        schema = spec.openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == spec.name
        assert schema["function"]["parameters"]["type"] == "object"


async def test_tool_loop_executes_tools_and_extracts_citations(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scholarrag.retrieval.service import RetrievalService

    settings = Settings(data_dir=tmp_path / "data")
    chunk = make_chunk("chk_1")

    async def fake_retrieve(self, query, *, document_ids=None, top_k=8, events=None):
        return [RetrievedChunk(chunk=chunk, score=0.9)], {"dense_candidates": 1}

    monkeypatch.setattr(RetrievalService, "retrieve", fake_retrieve)

    turns = [
        # Turn 1: thinking + one retrieve tool call, no content.
        (
            "",
            [{"id": "call_1", "name": "retrieve", "arguments": '{"query": "核心方法"}'}],
            {"total_tokens": 40},
            "provider-trace-1",
        ),
        # Turn 2: final answer citing [C1], no tool calls.
        ("该论文的核心方法使用混合检索与重排 [C1]。", [], {"total_tokens": 80}, "provider-trace-2"),
    ]

    async def fake_chat_with_tools(self, provider, messages, tool_schemas, *, events=None):
        assert len(tool_schemas) == 4
        return turns.pop(0)

    monkeypatch.setattr(ToolAgentService, "_chat_with_tools", fake_chat_with_tools)

    service = ToolAgentService(settings)
    answer = await service.answer("这篇论文的核心方法是什么？", top_k=8)

    assert answer.answer == "该论文的核心方法使用混合检索与重排 [C1]。"
    assert len(answer.citations) == 1
    assert answer.citations[0].citation_id == "C1"
    assert answer.citations[0].chunk_id == "chk_1"
    assert answer.citations[0].page_number == 3
    assert answer.retrieval_debug["agent_stop_reason"] == "tool_loop_complete"
    steps = answer.retrieval_debug["agent_steps"]
    assert len(steps) == 1
    assert steps[0]["name"] == "retrieve"
    assert json.loads(steps[0]["arguments"]) == {"query": "核心方法"}
    assert "1 个候选证据" in steps[0]["result_summary"]
    assert answer.retrieval_debug["retrieved_chunk_ids"] == ["chk_1"]


async def test_tool_loop_respects_call_budget(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scholarrag.rag.tool_agent as tool_agent_module
    from scholarrag.retrieval.service import RetrievalService

    settings = Settings(
        data_dir=tmp_path / "data",
        agent_tool_max_calls=2,
    )
    chunk = make_chunk("chk_1")

    async def fake_retrieve(self, query, *, document_ids=None, top_k=8, events=None):
        return [RetrievedChunk(chunk=chunk, score=0.9)], {}

    monkeypatch.setattr(RetrievalService, "retrieve", fake_retrieve)

    async def endless_tools(self, provider, messages, tool_schemas, *, events=None):
        return (
            "",
            [{"id": f"call_{len(messages)}", "name": "retrieve", "arguments": '{"query": "x"}'}],
            {},
            None,
        )

    async def fake_generation(provider, messages, *, events=None, model=None):
        return "预算耗尽后基于已有证据的回答 [C1]。"

    monkeypatch.setattr(ToolAgentService, "_chat_with_tools", endless_tools)
    monkeypatch.setattr(tool_agent_module, "_stream_generation", fake_generation)

    service = ToolAgentService(settings)
    answer = await service.answer("一直检索会怎样？", top_k=8)

    # Budget exhausted: synthesis still happens with collected evidence.
    assert answer.retrieval_debug["agent_stop_reason"] == "tool_budget_exhausted"
    assert len(answer.retrieval_debug["agent_steps"]) == 2
    assert answer.answer == "预算耗尽后基于已有证据的回答 [C1]。"
    assert len(answer.citations) == 1  # evidence was registered before the budget ran out


async def test_tool_agent_falls_back_to_controlled_rag_on_provider_error(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    fallback = Answer(
        answer="受控 RAG 兜底回答 [C1]。",
        citations=[],
        trace_id="trace-fallback",
    )

    async def raise_error(self, provider, messages, tool_schemas, *, events=None):
        raise RuntimeError("provider exploded")

    async def fake_controlled(self, question, *args, **kwargs):
        result = fallback.model_copy(deep=True)
        result.retrieval_debug["agent_stop_reason"] = "fallback_controlled_rag"
        return result

    monkeypatch.setattr(ToolAgentService, "_chat_with_tools", raise_error)
    monkeypatch.setattr(ToolAgentService, "_fallback", fake_controlled)

    service = ToolAgentService(settings)
    answer = await service.answer("出错了怎么办？")

    assert answer.answer == fallback.answer
    assert answer.retrieval_debug["agent_stop_reason"] == "fallback_controlled_rag"


async def test_agentic_service_routes_complex_questions_to_tool_agent(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scholarrag.rag.agent import AgenticRAGService

    settings = Settings(data_dir=tmp_path / "data")
    plan = AgenticRAGService.plan("比较两篇论文的方法与实验结果。", max_steps=4)
    assert plan.mode == "multi_step"

    captured: dict[str, object] = {}

    async def fake_tool_answer(question, *, document_ids=None, top_k=8, events=None, trace_id=None):
        captured["question"] = question
        return Answer(
            answer="工具模式回答 [C1]。",
            citations=[],
            trace_id="trace-tool",
            retrieval_debug={"agent_stop_reason": "tool_loop_complete"},
        )

    service = AgenticRAGService(settings)
    service.tool_agent = SimpleNamespace(answer=fake_tool_answer)  # type: ignore[attr-defined]

    answer = await service.answer("比较两篇论文的方法与实验结果。")
    assert captured["question"] == "比较两篇论文的方法与实验结果。"
    assert answer.retrieval_debug["agent_stop_reason"] == "tool_loop_complete"


async def test_agentic_service_respects_tool_calling_override(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scholarrag.rag.agent import AgenticRAGService

    settings = Settings(data_dir=tmp_path / "data")
    service = AgenticRAGService(settings, tool_calling=False)
    assert service.tool_calling is False
    service_default = AgenticRAGService(settings)
    assert service_default.tool_calling is settings.agent_tool_calling
