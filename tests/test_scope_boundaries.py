from __future__ import annotations

import pytest

from scholarrag.config import Settings
from scholarrag.domain.models import CanonicalDocument, Chunk, RetrievedChunk, SourcePointer
from scholarrag.rag.scope import DocumentScope
from scholarrag.rag.tools import EvidenceRegistry, ToolRuntime, build_tool_specs
from scholarrag.retrieval.index import VectorIndex, VisualIndex
from scholarrag.retrieval.service import RetrievalService
from scholarrag.storage.catalog import Catalog


@pytest.fixture
def library(tmp_path):
    settings = Settings(data_dir=tmp_path)
    catalog = Catalog(settings)
    chunks = {}
    for name in ("allowed", "other"):
        document = CanonicalDocument(
            document_id=name,
            parse_id=f"parse-{name}",
            source_sha256="a" * 64,
            source_filename=f"{name}.pdf",
            provider="test",
            provider_model="test",
        )
        chunk = Chunk(
            id=f"chunk-{name}",
            document_id=name,
            parse_id=document.parse_id,
            text=f"evidence {name}",
            contextual_text=f"evidence {name}",
            block_ids=[name],
            page_numbers=[1],
            token_estimate=2,
            sources=[
                SourcePointer(
                    document_id=name,
                    parse_id=document.parse_id,
                    provider="test",
                    page_number=1,
                    raw_artifact="test.json",
                )
            ],
        )
        catalog.register(
            document, [chunk], source_path=tmp_path / name, artifact_dir=tmp_path / name
        )
        chunks[name] = chunk
    return settings, catalog, chunks


@pytest.mark.parametrize(
    ("selected", "authorized", "expected"),
    [
        (None, None, None),
        ([], None, []),
        (None, [], []),
        (["a", "b"], ["b", "c"], ["b"]),
        (["a"], ["b"], []),
    ],
)
def test_scope_intersection_preserves_empty(selected, authorized, expected):
    assert DocumentScope.intersect(selected, authorized).resolve() == expected


async def test_all_tools_enforce_selection_before_reading_content(library, monkeypatch):
    settings, catalog, chunks = library
    runtime = ToolRuntime(settings, EvidenceRegistry(), document_ids=["allowed"])
    tools = {tool.name: tool.handler for tool in build_tool_specs()}
    listed = await tools["list_papers"](runtime, {})
    assert [p["document_id"] for p in listed["papers"]] == ["allowed"]
    assert (await tools["get_paper"](runtime, {"document_id": "other"}))["paper"] is None
    assert (await tools["read_chunk"](runtime, {"chunk_id": "chunk-other"}))["chunk"] is None
    runtime.registry.register(RetrievedChunk(chunk=chunks["other"], score=1))
    assert (await tools["read_chunk"](runtime, {"chunk_id": "chunk-other"}))["chunk"] is None
    calls = []

    async def retrieve(query, **kwargs):
        calls.append(kwargs["document_ids"])
        return [RetrievedChunk(chunk=chunk, score=1) for chunk in chunks.values()], {}

    monkeypatch.setattr(runtime.retrieval, "retrieve", retrieve)
    with pytest.raises(PermissionError):
        await tools["retrieve"](runtime, {"query": "evidence", "document_ids": ["other"]})
    empty = await tools["retrieve"](runtime, {"query": "evidence", "document_ids": []})
    assert empty["candidates"] == [] and calls == []
    result = await tools["retrieve"](runtime, {"query": "evidence"})
    assert calls == [["allowed"]]
    assert [c["document_id"] for c in result["candidates"]] == ["allowed"]
    assert catalog.chunks_by_ids(["chunk-allowed", "chunk-other"], document_ids=[]) == {}


async def test_empty_scope_never_contacts_provider_or_returns_catalog_results(library, monkeypatch):
    settings, catalog, _ = library
    import scholarrag.retrieval.service as module

    def forbidden(*args, **kwargs):
        pytest.fail("Empty scope must not create a provider")

    monkeypatch.setattr(module, "SiliconFlowClient", forbidden)
    results, _ = await RetrievalService(settings).retrieve("evidence", document_ids=[])
    assert results == []
    assert catalog.active_parse_ids([]) == []
    assert catalog.lexical_search("evidence", document_ids=[]) == []
    assert catalog.knowledge_graph([]) == {"nodes": [], "edges": []}
    for index in (VectorIndex(settings), VisualIndex(settings)):
        monkeypatch.setattr(index, "_client", forbidden)
        assert await index.search([0.0], document_ids=[]) == []
        assert await index.search([0.0], parse_ids=[]) == []


async def test_authorized_scope_can_only_narrow_selection(library):
    settings, _, _ = library
    runtime = ToolRuntime(
        settings,
        EvidenceRegistry(),
        document_ids=["allowed", "other"],
        authorized_document_ids=["allowed"],
    )
    listed = await build_tool_specs()[0].handler(runtime, {})
    assert [p["document_id"] for p in listed["papers"]] == ["allowed"]
