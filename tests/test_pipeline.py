from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pymupdf
import pytest

from scholarrag.api.main import Runtime, _run_query
from scholarrag.benchmark.parsers import ParserBenchmarkRunner
from scholarrag.config import Settings
from scholarrag.domain.models import (
    Answer,
    BlockKind,
    BoundingBox,
    CanonicalDocument,
    Chunk,
    Citation,
    ContentBlock,
    Page,
    RetrievedChunk,
    SourcePointer,
)
from scholarrag.evaluation.dashboard import QualityDashboard
from scholarrag.evaluation.easydataset import EasyDatasetAdapter
from scholarrag.evaluation.human_review import HumanReviewError, HumanReviewService
from scholarrag.evaluation.jobs import EvaluationJobStore
from scholarrag.evaluation.parser_gold import ParserGoldPreparer, annotation_schema_path
from scholarrag.evaluation.service import EvaluationCase, RagasEvaluator, load_cases
from scholarrag.ingestion.chunker import SemanticChunker
from scholarrag.ingestion.mineru_normalizer import MinerUNormalizer
from scholarrag.ingestion.paddleocr_normalizer import PaddleOCRNormalizer
from scholarrag.ingestion.quality import ParseQualityGate
from scholarrag.ingestion.review import ParseReviewService
from scholarrag.ingestion.routing import ParseRouter
from scholarrag.ingestion.service import IngestionService, ParseCandidate
from scholarrag.maintenance import BackupService
from scholarrag.providers.paddleocr import PaddleOCRAPIClient
from scholarrag.providers.siliconflow import SiliconFlowClient
from scholarrag.rag.agent import AgenticRAGService
from scholarrag.rag.runs import PipelineTracer, QueryRunStore, utc_now
from scholarrag.rag.service import RAGService
from scholarrag.retrieval.index import VectorIndex, VisualIndex
from scholarrag.retrieval.service import RetrievalService
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog


def make_mineru_fixture(root: Path) -> None:
    content = [
        [
            {
                "type": "title",
                "content": {
                    "title_content": [{"type": "text", "content": "A Traceable Paper"}],
                    "level": 1,
                },
                "bbox": [100, 80, 900, 130],
            },
            {
                "type": "title",
                "content": {
                    "title_content": [{"type": "text", "content": "Abstract"}],
                    "level": 2,
                },
                "bbox": [100, 160, 300, 190],
            },
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"type": "text", "content": "Evidence remains linked to its source."}
                    ]
                },
                "bbox": [100, 200, 900, 320],
            },
        ],
        [
            {
                "type": "equation_interline",
                "content": {"math_content": "E = mc^2", "math_type": "latex"},
                "bbox": [300, 300, 700, 360],
            }
        ],
    ]
    (root / "fixture_content_list_v2.json").write_text(json.dumps(content), encoding="utf-8")
    (root / "layout.json").write_text(
        json.dumps(
            {
                "pdf_info": [
                    {"page_idx": 0, "page_size": [612, 792]},
                    {"page_idx": 1, "page_size": [612, 792]},
                ]
            }
        ),
        encoding="utf-8",
    )


def make_high_quality_document(
    *,
    parse_id: str = "parse_quality",
    phrase: str = "grounded evidence",
) -> CanonicalDocument:
    blocks: list[ContentBlock] = []
    pages: list[Page] = []
    for page_number in (1, 2):
        block_id = f"{parse_id}-block-{page_number}"
        blocks.append(
            ContentBlock(
                id=block_id,
                kind=BlockKind.PARAGRAPH,
                text=(f"Page {page_number} {phrase}. " * 80),
                reading_order=page_number - 1,
                source=SourcePointer(
                    document_id="doc_quality",
                    parse_id=parse_id,
                    provider="test",
                    provider_block_id=block_id,
                    page_number=page_number,
                    bbox=BoundingBox(
                        x0=100,
                        y0=100,
                        x1=900,
                        y1=900,
                        coordinate_space="normalized_0_1000",
                    ),
                    raw_artifact="/immutable/provider_response.json",
                    raw_json_pointer=f"/pages/{page_number - 1}/blocks/0",
                ),
            )
        )
        pages.append(Page(page_number=page_number, block_ids=[block_id]))
    return CanonicalDocument(
        document_id="doc_quality",
        parse_id=parse_id,
        source_sha256="a" * 64,
        source_filename="quality.pdf",
        provider="test",
        provider_model="test-v1",
        pages=pages,
        blocks=blocks,
    )


def test_normalize_chunk_and_catalog(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    make_mineru_fixture(raw_dir)
    document = MinerUNormalizer().normalize(
        raw_dir,
        document_id="doc_test",
        parse_id="parse_test",
        source_sha256="a" * 64,
        source_filename="paper.pdf",
        provider_model="vlm",
    )
    chunks = SemanticChunker(max_chars=1000).chunk(document)

    assert document.title == "A Traceable Paper"
    assert document.abstract == "Evidence remains linked to its source."
    assert document.blocks[-1].latex == "E = mc^2"
    assert document.blocks[-1].source.page_number == 2
    assert document.blocks[-1].source.raw_json_pointer == "/1/0"
    assert chunks and chunks[0].sources[0].bbox is not None

    settings = Settings(data_dir=tmp_path / "data")
    source = tmp_path / "paper.pdf"
    source.touch()
    Catalog(settings).register(
        document, chunks, source_path=source, artifact_dir=tmp_path / "artifacts"
    )
    results = Catalog(settings).lexical_search("traceable evidence")
    assert results
    loaded = Catalog(settings).chunks_by_ids([results[0][0]])
    assert loaded[results[0][0]].document_id == "doc_test"


def test_rrf_rewards_overlap() -> None:
    fused = RetrievalService._rrf(
        [("dense-only", 0.9), ("both", 0.8)],
        [("both", 3.0), ("lexical-only", 2.0)],
    )
    assert fused[0][0] == "both"


def test_dashboard_p95_never_falls_below_median_for_small_samples() -> None:
    distribution = QualityDashboard._distribution([9.744, 49.094])
    assert distribution["p50"] == 29.419
    assert distribution["p95"] == 49.094


def test_embedded_qdrant_uses_one_process_client_for_parallel_indexes(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    indexes = [VectorIndex(settings), VisualIndex(settings)] * 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        clients = list(pool.map(lambda index: index._client(), indexes))
    assert len({id(client) for client in clients}) == 1


def test_agent_planner_is_bounded_and_routes_simple_questions() -> None:
    simple = AgenticRAGService.plan("论文发表在哪一年？", max_steps=4)
    assert simple.mode == "controlled_rag"
    comparison = AgenticRAGService.plan("比较两篇论文的方法、实验结果以及各自局限。", max_steps=3)
    assert comparison.mode == "multi_step"
    assert comparison.intent == "comparison"
    assert len(comparison.subqueries) == 3
    assert (
        AgenticRAGService._context_budget(
            comparison,
            requested_top_k=8,
            document_count=4,
        )
        == 12
    )


def test_diversity_selection_balances_documents_and_sections() -> None:
    def item(chunk_id: str, document_id: str, section: str, score: float) -> RetrievedChunk:
        source = SourcePointer(
            document_id=document_id,
            parse_id="parse",
            provider="test",
            page_number=1,
            raw_artifact="raw.json",
        )
        return RetrievedChunk(
            chunk=Chunk(
                id=chunk_id,
                document_id=document_id,
                parse_id="parse",
                text=chunk_id,
                contextual_text=chunk_id,
                block_ids=[f"block-{chunk_id}"],
                page_numbers=[1],
                section_path=[section],
                token_estimate=1,
                sources=[source],
            ),
            score=score,
        )

    results = [
        item("a1", "doc-a", "method", 1.0),
        item("a2", "doc-a", "method", 0.9),
        item("a3", "doc-a", "method", 0.8),
        item("b1", "doc-b", "results", 0.7),
        item("b2", "doc-b", "limits", 0.6),
    ]
    selected = RetrievalService._diversify(results, top_k=4)
    assert {row.chunk.document_id for row in selected[:3]} == {"doc-a", "doc-b"}
    assert sum(row.chunk.document_id == "doc-a" for row in selected) <= 2

    comparison_results = results + [
        item("c1", "doc-c", "method", 0.5),
        item("d1", "doc-d", "method", 0.4),
    ]
    covered = AgenticRAGService._select_with_document_coverage(
        comparison_results,
        top_k=4,
        expected_documents={"doc-a", "doc-b", "doc-c", "doc-d"},
    )
    assert {row.chunk.document_id for row in covered} == {
        "doc-a",
        "doc-b",
        "doc-c",
        "doc-d",
    }


def test_hierarchical_chunks_index_only_retrievable_children(tmp_path: Path) -> None:
    document = make_high_quality_document()
    chunks = SemanticChunker(max_chars=300, parent_max_chars=10_000).chunk(document)
    parents = [chunk for chunk in chunks if chunk.level == "parent"]
    children = [chunk for chunk in chunks if chunk.level == "child"]
    assert len(parents) == 1
    assert children
    assert {chunk.parent_chunk_id for chunk in children} == {parents[0].id}

    settings = Settings(data_dir=tmp_path / "data")
    source = tmp_path / "quality.pdf"
    source.touch()
    catalog = Catalog(settings)
    catalog.register(document, chunks, source_path=source, artifact_dir=tmp_path)
    lexical_ids = {chunk_id for chunk_id, _ in catalog.lexical_search("grounded evidence")}
    assert parents[0].id not in lexical_ids
    assert lexical_ids & {chunk.id for chunk in children}


def test_claims_link_to_minimal_evidence_spans() -> None:
    source = SourcePointer(
        document_id="doc_1",
        parse_id="parse_1",
        provider="test",
        page_number=3,
        bbox=BoundingBox(x0=1, y0=2, x1=3, y1=4),
        raw_artifact="raw.json",
    )
    chunk = Chunk(
        id="chunk_1",
        document_id="doc_1",
        parse_id="parse_1",
        text="The method improves recall by 12.5%. It uses a two-stage retriever.",
        contextual_text="The method improves recall by 12.5%. It uses a two-stage retriever.",
        block_ids=["block_1"],
        page_numbers=[3],
        token_estimate=20,
        sources=[source],
    )
    claims = RAGService._claims(
        "该方法将召回率提高 12.5% [C1]。",
        [RetrievedChunk(chunk=chunk, score=0.9)],
    )
    assert len(claims) == 1
    assert claims[0].supported is False
    assert claims[0].evidence_located is True
    assert claims[0].support_status == "not_checked"
    assert claims[0].citation_ids == ["C1"]
    assert "12.5%" in claims[0].evidence_spans[0].quote
    assert claims[0].evidence_spans[0].block_id == "block_1"


def test_citation_skips_heading_blocks(tmp_path: Path) -> None:
    """Citations anchor on the first content block, not the section heading."""
    settings = Settings(data_dir=tmp_path / "data")
    catalog = Catalog(settings)
    parse_dir = tmp_path / "artifacts" / "parse_1"
    canonical_dir = parse_dir / "canonical"
    canonical_dir.mkdir(parents=True)
    (canonical_dir / "document.json").write_text(
        json.dumps(
            {
                "blocks": [
                    {
                        "id": "blk_heading",
                        "kind": "heading",
                        "text": "3 KNOWLEDGE-INTENSIVE REASONING TASKS",
                    },
                    {
                        "id": "blk_body",
                        "kind": "paragraph",
                        "text": "As shown in Figure 1(1d), by interacting…",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    document = make_high_quality_document(parse_id="parse_1")
    chunks = SemanticChunker(max_chars=10_000).chunk(document)
    catalog.register(
        document,
        chunks,
        source_path=tmp_path / "paper.pdf",
        artifact_dir=parse_dir,
    )

    heading_source = SourcePointer(
        document_id="doc_quality",
        parse_id="parse_1",
        provider="test",
        page_number=4,
        bbox=BoundingBox(x0=171, y0=369, x1=584, y1=385, coordinate_space="normalized_0_1000"),
        raw_artifact="raw.json",
    )
    body_source = SourcePointer(
        document_id="doc_quality",
        parse_id="parse_1",
        provider="test",
        page_number=4,
        bbox=BoundingBox(x0=169, y0=401, x1=826, y1=459, coordinate_space="normalized_0_1000"),
        raw_artifact="raw.json",
    )
    chunk = Chunk(
        id="chunk_1",
        document_id="doc_quality",
        parse_id="parse_1",
        text="3 KNOWLEDGE-INTENSIVE REASONING TASKS\n\nAs shown in Figure 1(1d), by interacting…",
        contextual_text="3 KNOWLEDGE-INTENSIVE REASONING TASKS\n\nAs shown in Figure 1(1d), by interacting…",
        block_ids=["blk_heading", "blk_body"],
        page_numbers=[4],
        token_estimate=20,
        sources=[heading_source, body_source],
    )
    item = RetrievedChunk(chunk=chunk, score=0.9)

    # With catalog: skip the heading, land on the content block (same anchor
    # the claim spans use).
    citation = RAGService._citation(item, 5, catalog=catalog)
    assert citation.bbox == body_source.bbox
    assert citation.page_number == 4
    assert citation.block_ids == ["blk_heading", "blk_body"]

    # Without catalog: fall back to the chunk's first source (heading).
    fallback = RAGService._citation(item, 5)
    assert fallback.bbox == heading_source.bbox


def test_citation_validity_compares_normalized_citation_ids() -> None:
    answer = Answer(
        answer="A grounded fact [C1].",
        citations=[
            Citation(
                citation_id="C1",
                document_id="doc_1",
                chunk_id="chunk_1",
                page_number=1,
                block_ids=["block_1"],
                quote="A grounded fact.",
            )
        ],
        trace_id="trace",
    )
    metrics = RagasEvaluator._retrieval_metrics(
        retrieved_chunk_ids=["chunk_1"],
        expected_chunk_ids=[],
        answer=answer,
    )
    assert metrics["citation_validity"].value == 1.0


async def test_query_run_persists_ordered_pipeline_events(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    store = QueryRunStore(settings)
    run = await store.create(question="What is the contribution?", document_ids=["doc_1"], top_k=8)
    await store.update(run.run_id, status="running", started_at=utc_now())
    pipeline = PipelineTracer(store.recorder(run.run_id))

    async with pipeline.stage(
        "retrieval.lexical",
        "关键词召回",
        component="sqlite-fts5",
        input={"limit": 30},
    ) as stage:
        stage.complete(
            output={"candidate_count": 2},
            summary="2 个候选",
        )

    current = await store.update(run.run_id, status="completed", completed_at=utc_now())
    events = store.list_events(run.run_id)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert [event.type for event in events] == [
        "run.created",
        "stage.started",
        "stage.completed",
    ]
    assert events[-1].output["candidate_count"] == 2
    assert events[-1].duration_ms is not None
    assert current.status == "completed"
    assert QueryRunStore(settings).list_events(run.run_id) == events
    assert store.list()[0].run_id == run.run_id


async def test_query_run_recovers_interrupted_snapshot(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    store = QueryRunStore(settings)
    run = await store.create(question="Interrupted query", document_ids=None, top_k=8)
    await store.update(run.run_id, status="running", started_at=utc_now())

    recovered = await QueryRunStore(settings).recover_interrupted()

    assert recovered == [run.run_id]
    current = store.get(run.run_id)
    assert current and current.status == "failed"
    assert "restarted" in (current.error or "")
    assert store.list_events(run.run_id)[-1].type == "run.failed"


async def test_query_run_recovers_interrupted_evaluation_child_stage(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    store = QueryRunStore(settings)
    run = await store.create(question="Completed query", document_ids=None, top_k=8)
    await store.update(run.run_id, status="completed", completed_at=utc_now())
    await store.recorder(run.run_id).emit(
        event_type="stage.started",
        stage_id="evaluation.faithfulness",
        label="评估 faithfulness",
        status="running",
        parent_stage_id="evaluation.ragas",
    )

    recovered = await QueryRunStore(settings).recover_interrupted_child_stages()

    assert recovered == [f"{run.run_id}:evaluation.faithfulness"]
    final = store.list_events(run.run_id)[-1]
    assert final.type == "evaluation.interrupted"
    assert final.status == "warning"
    assert store.get(run.run_id).status == "completed"  # type: ignore[union-attr]


async def test_evaluation_jobs_are_async_deduplicated_and_recoverable(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    query_runs = QueryRunStore(settings)
    query_run = await query_runs.create(question="What is ReAct?", document_ids=None, top_k=8)
    await query_runs.update(query_run.run_id, status="completed", completed_at=utc_now())
    jobs = EvaluationJobStore(settings)
    case = EvaluationCase(question=query_run.question)
    fingerprint = jobs.fingerprint(
        case,
        query_run_id=query_run.run_id,
        judge_model=settings.ragas_judge_model,
        judge_enable_thinking=settings.ragas_judge_enable_thinking,
    )

    first, created = await jobs.create_or_get_active(
        case=case,
        query_run_id=query_run.run_id,
        fingerprint=fingerprint,
    )
    equivalent_case = EvaluationCase(question=query_run.question)
    equivalent_fingerprint = jobs.fingerprint(
        equivalent_case,
        query_run_id=query_run.run_id,
        judge_model=settings.ragas_judge_model,
        judge_enable_thinking=settings.ragas_judge_enable_thinking,
    )
    duplicate, duplicate_created = await jobs.create_or_get_active(
        case=equivalent_case,
        query_run_id=query_run.run_id,
        fingerprint=equivalent_fingerprint,
    )
    assert created is True
    assert equivalent_case.case_id != case.case_id
    assert equivalent_fingerprint == fingerprint
    assert duplicate_created is False
    assert duplicate.job_id == first.job_id

    await jobs.start(first.job_id)
    recorder = jobs.recorder(first.job_id, parent=query_runs.recorder(query_run.run_id))
    await recorder.emit(
        event_type="stage.started",
        stage_id="evaluation.faithfulness",
        label="评估 faithfulness",
        status="running",
    )
    job_events = jobs.list_events(first.job_id)
    assert job_events[-1].stage_id == "evaluation.faithfulness"
    assert job_events[-1].trace["evaluation_job_id"] == first.job_id
    assert query_runs.list_events(query_run.run_id)[-1].trace["evaluation_job_id"] == first.job_id

    recovered = await EvaluationJobStore(settings).recover_interrupted()
    assert recovered == [first.job_id]
    failed = jobs.get(first.job_id)
    assert failed and failed.status == "failed"
    assert jobs.list_events(first.job_id)[-1].type == "evaluation.failed"

    retry, retry_created = await jobs.create_or_get_active(
        case=case,
        query_run_id=query_run.run_id,
        fingerprint=fingerprint,
    )
    assert retry_created is True
    assert retry.job_id != first.job_id
    assert jobs.latest_for_query_run(query_run.run_id).job_id == retry.job_id  # type: ignore[union-attr]


async def test_query_run_completes_without_provider_calls_when_scope_is_empty(
    tmp_path: Path,
) -> None:
    # Pin the controlled workflow: with tool calling enabled the request is
    # routed to the tool loop, which intentionally owns the empty-scope path.
    settings = Settings(
        data_dir=tmp_path / "data",
        agent_tool_calling=False,
    )
    runtime = Runtime(settings)
    run = await runtime.query_runs.create(
        question="What evidence is available?",
        document_ids=[],
        top_k=8,
    )

    await _run_query(runtime, run.run_id)

    completed = runtime.query_runs.get(run.run_id)
    assert completed and completed.status == "completed"
    assert completed.answer and completed.answer.citations == []
    events = runtime.query_runs.list_events(run.run_id)
    assert events[-1].type == "run.completed"
    skipped = {event.stage_id for event in events if event.status == "skipped"}
    assert "query.embedding.vl" in skipped
    assert "generation.answer" in skipped


def test_citation_coverage() -> None:
    response = "First supported claim [C1].\nSecond supported claim [C2][C3]."
    assert RagasEvaluator._citation_coverage(response) == 1.0
    assert RagasEvaluator._citation_coverage("Unsupported factual sentence.") == 0.0


def test_easydataset_import_keeps_candidates_out_of_reviewed_gold(tmp_path: Path) -> None:
    source = tmp_path / "easy.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "instruction": "What is the main contribution?",
                        "output": "The paper introduces a traceable RAG workflow.",
                        "labels": ["method"],
                        "source_chunk_ids": ["chunk_1"],
                    }
                ),
                json.dumps({"instruction": "missing answer"}),
            ]
        ),
        encoding="utf-8",
    )
    target = tmp_path / "scholarrag.jsonl"

    report = EasyDatasetAdapter.import_file(
        source,
        target,
        default_document_ids=["doc_1"],
    )

    cases = load_cases(target)
    assert report["imported"] == 1
    assert len(report["rejected"]) == 1
    assert cases[0].review_status == "candidate"
    assert cases[0].document_ids == ["doc_1"]
    assert cases[0].source_chunk_ids == ["chunk_1"]


async def test_siliconflow_stream_collects_deltas_usage_and_trace(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["model"] == "Qwen/Qwen3.6-27B"
        assert payload["enable_thinking"] is True
        # Qwen3 official thinking-mode sampling defaults (never temperature=0).
        assert payload["temperature"] == 0.6
        assert payload["top_p"] == 0.95
        assert payload["top_k"] == 20
        body = (
            'data: {"choices":[{"delta":{"content":"hello "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"world"}}],'
            '"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            text=body,
            headers={"x-siliconcloud-trace-id": "trace-stream"},
        )

    settings = Settings(
        data_dir=tmp_path / "data",
        siliconflow_api_key="test-key",
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SiliconFlowClient(settings, client=http_client)
    try:
        async with client.chat_stream([{"role": "user", "content": "hi"}]) as stream:
            chunks = [chunk async for chunk in stream.chunks()]
        assert "".join(chunk["choices"][0]["delta"]["content"] for chunk in chunks) == "hello world"
        assert stream.usage["completion_tokens"] == 2
        assert stream.trace_id == "trace-stream"
    finally:
        await http_client.aclose()


async def test_siliconflow_embeddings_use_content_cache(tmp_path: Path) -> None:
    calls = 0
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [float(index), 1.0]} for index, _ in enumerate(payload["input"])
                ],
                "usage": {"prompt_tokens": 2},
            },
        )

    settings = Settings(
        data_dir=tmp_path / "data",
        siliconflow_api_key="test-key",
        provider_max_retries=0,
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SiliconFlowClient(settings, client=http_client)
    try:
        first = await client.embeddings(["same text"])
        second = await client.embeddings(["same text"])
        assert first.data["cache"] == {"hits": 0, "misses": 1}
        assert second.data["cache"] == {"hits": 1, "misses": 0}
        assert second.data["data"][0]["embedding"] == [0.0, 1.0]
        assert calls == 1
        assert requests[0]["model"] == "Qwen/Qwen3-VL-Embedding-8B"
        assert requests[0]["dimensions"] == 1024
    finally:
        await http_client.aclose()


async def test_siliconflow_vl_rerank_accepts_mixed_text_and_image_documents(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            headers={"x-siliconcloud-trace-id": "trace-vl-rerank"},
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.8},
                ],
                "meta": {"tokens": {"image_tokens": 42}},
            },
        )

    settings = Settings(
        data_dir=tmp_path / "data",
        siliconflow_api_key="test-key",
        provider_max_retries=0,
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SiliconFlowClient(settings, client=http_client)
    try:
        response = await client.rerank(
            "find the matching figure",
            ["text candidate", {"image": "data:image/png;base64,aW1hZ2U="}],
            top_n=2,
        )
        assert response.trace_id == "trace-vl-rerank"
        assert requests == [
            {
                "model": "Qwen/Qwen3-VL-Reranker-8B",
                "query": "find the matching figure",
                "documents": [
                    "text candidate",
                    {"image": "data:image/png;base64,aW1hZ2U="},
                ],
                "return_documents": False,
                "top_n": 2,
            }
        ]
    finally:
        await http_client.aclose()


def test_vl_rerank_builder_adds_traceable_image_input(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    artifact_dir = settings.artifacts_dir / "doc_quality" / "parses" / "parse_quality"
    image_path = artifact_dir / "raw" / "figure.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image-bytes")
    document = make_high_quality_document()
    document.blocks[0].kind = BlockKind.FIGURE
    document.blocks[0].image_path = str(image_path)
    chunks = SemanticChunker(max_chars=10_000).chunk(document)
    canonical_dir = artifact_dir / "canonical"
    canonical_dir.mkdir(parents=True)
    (canonical_dir / "document.json").write_text(document.model_dump_json(), encoding="utf-8")
    source = tmp_path / "paper.pdf"
    source.touch()
    Catalog(settings).register(document, chunks, source_path=source, artifact_dir=artifact_dir)

    child = next(chunk for chunk in chunks if chunk.level != "parent")
    documents, refs, diagnostics = RetrievalService(settings)._build_vl_rerank_documents(
        [child.id], {child.id: child}
    )

    assert diagnostics == []
    assert refs == [
        {"chunk_id": child.id, "modality": "text"},
        {"chunk_id": child.id, "modality": "image"},
    ]
    assert documents[0] == child.contextual_text
    assert isinstance(documents[1], dict)
    assert documents[1]["image"].startswith("data:image/png;base64,")


def test_retrieval_embeddings_share_one_vl_model_and_dimension() -> None:
    settings = Settings(_env_file=None)
    assert settings.embedding_model == "Qwen/Qwen3-VL-Embedding-8B"
    assert settings.unified_embedding_model == settings.multimodal_embedding_model
    assert settings.embedding_dimensions == 1024
    assert settings.rerank_model == "Qwen/Qwen3-VL-Reranker-8B"
    assert VectorIndex.collection == "scholarrag_chunks_vl_v2"


def test_chat_and_ragas_judge_share_model_thinking_and_no_output_limit() -> None:
    settings = Settings(_env_file=None)
    assert settings.chat_model == "Qwen/Qwen3.6-27B"
    assert settings.chat_enable_thinking is True
    assert settings.ragas_judge_model == "Qwen/Qwen3.6-27B"
    assert settings.ragas_judge_context_window_tokens == 262_144
    assert settings.ragas_judge_enable_thinking is True
    # Qwen3 official thinking-mode defaults: temperature=0.6, top_p=0.95,
    # top_k=20, min_p=0. Greedy decoding (temperature=0) is forbidden.
    assert settings.chat_temperature == 0.6
    assert settings.chat_top_p == 0.95
    assert settings.chat_top_k == 20
    assert settings.chat_min_p == 0.0
    assert settings.ragas_judge_temperature == 0.6

    llm = SimpleNamespace(
        model_args={
            "temperature": 0.6,
            "max_tokens": 1024,
            "max_completion_tokens": 2048,
            "max_output_tokens": 4096,
        }
    )
    RagasEvaluator._remove_judge_output_limits(llm)
    assert llm.model_args == {"temperature": 0.6}


def test_parse_quality_gate_accepts_review_and_fallback_routes() -> None:
    gate = ParseQualityGate()
    document = make_high_quality_document()

    accepted = gate.evaluate(document, expected_page_count=2)
    assert accepted.decision == "accept"
    assert accepted.recommended_action == "activate"
    assert accepted.score == 1.0

    without_bbox = document.model_copy(deep=True)
    without_bbox.blocks[0].source.bbox = None
    review = gate.evaluate(without_bbox, expected_page_count=2)
    assert review.decision == "review"
    assert review.recommended_action == "manual_review"

    incomplete = gate.evaluate(document, expected_page_count=3)
    assert incomplete.decision == "fallback"
    assert incomplete.recommended_action == "run_fallback"
    assert "page_coverage" in incomplete.critical_failures


def test_parse_router_runs_fallback_compares_and_requires_review_on_disagreement() -> None:
    gate = ParseQualityGate()
    router = ParseRouter(min_score_improvement=0.03)
    primary_document = make_high_quality_document(parse_id="parse_primary")
    fallback_document = make_high_quality_document(parse_id="parse_fallback")
    primary_document.blocks[0].source.bbox = None
    primary = gate.evaluate(primary_document, expected_page_count=2)
    fallback = gate.evaluate(fallback_document, expected_page_count=2)

    assert router.should_run_fallback("on_review", primary) is True
    assert router.should_run_fallback("on_fallback", primary) is False
    decision = router.route(
        primary,
        fallback,
        primary_document=primary_document,
        fallback_document=fallback_document,
    )
    assert decision.recommended_action == "activate_fallback"
    assert decision.selected_parse_id == "parse_fallback"
    assert decision.comparison is not None
    assert decision.comparison.overall_similarity == 1.0

    unrelated_document = make_high_quality_document(
        parse_id="parse_unrelated", phrase="unrelated alternate vocabulary"
    )
    unrelated = gate.evaluate(unrelated_document, expected_page_count=2)
    disagreement = router.route(
        fallback,
        unrelated,
        primary_document=fallback_document,
        fallback_document=unrelated_document,
    )
    assert disagreement.recommended_action == "manual_review"
    assert disagreement.selected_parse_id is None
    assert disagreement.comparison is not None
    assert "low_page_text_similarity" in disagreement.comparison.disagreement_flags


def test_catalog_search_and_chunk_lookup_only_use_active_parse(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    catalog = Catalog(settings)
    source = tmp_path / "paper.pdf"
    source.touch()
    old_document = make_high_quality_document(parse_id="parse_old", phrase="obsoleteevidence")
    old_chunks = SemanticChunker(max_chars=10_000).chunk(old_document)
    catalog.register(
        old_document,
        old_chunks,
        source_path=source,
        artifact_dir=tmp_path / "old",
    )
    new_document = make_high_quality_document(parse_id="parse_new", phrase="currentevidence")
    new_chunks = SemanticChunker(max_chars=10_000).chunk(new_document)
    catalog.register(
        new_document,
        new_chunks,
        source_path=source,
        artifact_dir=tmp_path / "new",
    )

    assert catalog.active_parse_ids() == ["parse_new"]
    assert catalog.lexical_search("obsoleteevidence") == []
    assert catalog.lexical_search("currentevidence")
    assert catalog.chunks_by_ids([old_chunks[0].id]) == {}
    assert new_chunks[0].id in catalog.chunks_by_ids([new_chunks[0].id])


def test_vector_search_filters_parse_id(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    old_chunk = SemanticChunker(max_chars=10_000).chunk(
        make_high_quality_document(parse_id="parse_old", phrase="old vector")
    )[0]
    new_chunk = SemanticChunker(max_chars=10_000).chunk(
        make_high_quality_document(parse_id="parse_new", phrase="new vector")
    )[0]
    index = VectorIndex(settings)
    index._upsert([old_chunk, new_chunk], [[1.0, 0.0], [1.0, 0.0]])

    results = index._search([1.0, 0.0], None, ["parse_new"], 10)
    assert results == [(new_chunk.id, 1.0)]


async def test_routed_ingestion_selects_fallback_before_activation(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        parse_fallback_mode="on_review",
    )
    service = IngestionService(settings)
    gate = ParseQualityGate()
    source = tmp_path / "paper.pdf"
    source.touch()
    primary_document = make_high_quality_document(parse_id="parse_primary")
    primary_document.blocks[0].source.bbox = None
    fallback_document = make_high_quality_document(parse_id="parse_fallback")
    primary_dir = tmp_path / "primary"
    fallback_dir = tmp_path / "fallback"
    primary_dir.mkdir()
    fallback_dir.mkdir()
    (primary_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (fallback_dir / "manifest.json").write_text("{}", encoding="utf-8")
    primary = ParseCandidate(
        primary_document,
        SemanticChunker(max_chars=10_000).chunk(primary_document),
        primary_dir,
        source,
        gate.evaluate(primary_document, expected_page_count=2),
    )
    fallback = ParseCandidate(
        fallback_document,
        SemanticChunker(max_chars=10_000).chunk(fallback_document),
        fallback_dir,
        source,
        gate.evaluate(fallback_document, expected_page_count=2),
    )
    activated: list[str] = []

    async def fake_primary(*args, **kwargs):
        return primary

    async def fake_fallback(*args, **kwargs):
        return fallback

    async def fake_activate(candidate, **kwargs):
        activated.append(candidate.document.parse_id)

    monkeypatch.setattr(service, "_parse_mineru_candidate", fake_primary)
    monkeypatch.setattr(service, "_parse_paddle_candidate", fake_fallback)
    monkeypatch.setattr(service, "_activate_candidate", fake_activate)

    document, _, _ = await service.ingest(source)
    assert document.parse_id == "parse_fallback"
    assert activated == ["parse_fallback"]
    assert (
        json.loads((primary_dir / "manifest.json").read_text(encoding="utf-8"))["status"]
        == "candidate_not_selected"
    )


async def test_manual_review_reactivates_registered_parse_without_reindexing(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    store = ArtifactStore(settings)
    catalog = Catalog(settings)
    source = store.document_dir("doc_quality") / "source" / "paper.pdf"
    source.parent.mkdir(parents=True)
    source.touch()

    def persist(document: CanonicalDocument):
        parse_dir = store.parse_dir(document.document_id, document.parse_id)
        chunks = SemanticChunker(max_chars=10_000).chunk(document)
        store.write_json(
            parse_dir / "canonical" / "document.json",
            document.model_dump(mode="json"),
        )
        store.write_json(
            parse_dir / "chunks" / "chunks.json",
            [chunk.model_dump(mode="json") for chunk in chunks],
        )
        store.write_json(
            parse_dir / "manifest.json",
            {
                "document_id": document.document_id,
                "parse_id": document.parse_id,
                "provider": document.provider,
                "provider_model": document.provider_model,
                "status": "ready",
                "preserved_source": str(source),
                "created_at": document.created_at.isoformat(),
                "page_count": len(document.pages),
                "block_count": len(document.blocks),
                "chunk_count": len(chunks),
                "quality": {"score": 1.0, "decision": "accept"},
            },
        )
        return parse_dir, chunks

    old_document = make_high_quality_document(parse_id="parse_old")
    new_document = make_high_quality_document(parse_id="parse_new")
    old_dir, old_chunks = persist(old_document)
    new_dir, new_chunks = persist(new_document)
    catalog.register(old_document, old_chunks, source_path=source, artifact_dir=old_dir)
    catalog.register(new_document, new_chunks, source_path=source, artifact_dir=new_dir)

    reviews = ParseReviewService(settings)
    result = await reviews.activate(
        "doc_quality",
        "parse_old",
        reason="人工检查公式与页面定位均正确",
    )
    assert result.previous_parse_id == "parse_new"
    assert result.active_parse_id == "parse_old"
    assert result.indexed is False
    assert catalog.active_parse_ids(["doc_quality"]) == ["parse_old"]
    assert Path(result.review_artifact).is_file()
    versions = reviews.list_versions("doc_quality")
    assert next(item for item in versions if item.parse_id == "parse_old").active is True


async def test_manual_review_requires_force_for_failed_quality_gate(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    store = ArtifactStore(settings)
    document = make_high_quality_document(parse_id="parse_rejected")
    parse_dir = store.parse_dir(document.document_id, document.parse_id)
    chunks = SemanticChunker(max_chars=10_000).chunk(document)
    source = store.document_dir(document.document_id) / "source" / "paper.pdf"
    source.parent.mkdir(parents=True)
    source.touch()
    store.write_json(parse_dir / "canonical" / "document.json", document.model_dump(mode="json"))
    store.write_json(
        parse_dir / "chunks" / "chunks.json",
        [chunk.model_dump(mode="json") for chunk in chunks],
    )
    store.write_json(
        parse_dir / "manifest.json",
        {
            "document_id": document.document_id,
            "parse_id": document.parse_id,
            "provider": document.provider,
            "provider_model": document.provider_model,
            "status": "fallback_required",
            "preserved_source": str(source),
            "created_at": document.created_at.isoformat(),
        },
    )
    store.write_json(
        parse_dir / "quality" / "report.json",
        {"score": 0.4, "decision": "fallback", "critical_failures": ["page_coverage"]},
    )

    reviews = ParseReviewService(settings)
    version = reviews.list_versions(document.document_id)[0]
    assert version.quality and version.quality["decision"] == "fallback"
    with pytest.raises(RuntimeError, match="force=true"):
        await reviews.activate(
            document.document_id,
            document.parse_id,
            reason="人工希望覆盖质量门禁",
        )


async def test_activation_failure_is_audited_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    store = ArtifactStore(settings)
    document = make_high_quality_document(parse_id="parse_retryable")
    parse_dir = store.parse_dir(document.document_id, document.parse_id)
    chunks = SemanticChunker(max_chars=10_000).chunk(document)
    source = store.document_dir(document.document_id) / "source" / "paper.pdf"
    source.parent.mkdir(parents=True)
    source.touch()
    store.write_json(parse_dir / "canonical" / "document.json", document.model_dump(mode="json"))
    store.write_json(
        parse_dir / "chunks" / "chunks.json",
        [chunk.model_dump(mode="json") for chunk in chunks],
    )
    manifest_path = parse_dir / "manifest.json"
    store.write_json(
        manifest_path,
        {
            "document_id": document.document_id,
            "parse_id": document.parse_id,
            "provider": document.provider,
            "provider_model": document.provider_model,
            "status": "candidate_ready",
            "preserved_source": str(source),
            "created_at": document.created_at.isoformat(),
            "quality": {"score": 1.0, "decision": "accept"},
        },
    )

    async def fail_index(*args, **kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(VectorIndex, "index", fail_index)
    reviews = ParseReviewService(settings)
    with pytest.raises(RuntimeError, match="index unavailable"):
        await reviews.activate(
            document.document_id,
            document.parse_id,
            reason="首次索引尝试",
        )

    failed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert failed_manifest["status"] == "activation_failed"
    assert failed_manifest["activation_attempts"][0]["status"] == "failed"
    assert Path(failed_manifest["activation_attempts"][0]["artifact"]).is_file()
    assert reviews.list_versions(document.document_id)[0].can_activate is True

    async def index_ok(self, values, **kwargs):
        return len(values)

    async def visual_index_ok(self, document, chunks, **kwargs):
        return 0

    monkeypatch.setattr(VectorIndex, "index", index_ok)
    monkeypatch.setattr(VisualIndex, "index", visual_index_ok)
    result = await reviews.activate(
        document.document_id,
        document.parse_id,
        reason="索引恢复后重试",
    )
    assert result.indexed is True
    ready_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert ready_manifest["status"] == "ready"
    assert "activation_error" not in ready_manifest
    assert [row["status"] for row in ready_manifest["activation_attempts"]] == [
        "failed",
        "success",
    ]


def test_paddle_result_serializes_nested_dataclasses() -> None:
    @dataclass
    class PageResult:
        markdown_text: str

    @dataclass
    class DocumentResult:
        job_id: str
        pages: list[PageResult]

    payload = PaddleOCRAPIClient.to_dict(DocumentResult("job-1", [PageResult("content")]))
    assert payload == {"job_id": "job-1", "pages": [{"markdown_text": "content"}]}


def test_paddle_resources_are_namespaced_by_page_and_nested_path(tmp_path: Path) -> None:
    class FakeClient:
        def save_resource(
            self,
            resource_url: str,
            destination: str,
            *,
            filename: str,
            overwrite: bool,
        ) -> str:
            assert resource_url.startswith("https://")
            assert overwrite is False
            target = Path(destination) / filename
            target.write_bytes(b"resource")
            return str(target)

    result = SimpleNamespace(
        pages=[
            SimpleNamespace(
                markdown_images={"imgs/chart.jpg": "https://example.test/chart-1"},
                output_images={"layout_det_res": "https://example.test/layout-1"},
            ),
            SimpleNamespace(
                markdown_images={"imgs/chart.jpg": "https://example.test/chart-2"},
                output_images={"layout_det_res": "https://example.test/layout-2"},
            ),
        ]
    )
    paths = PaddleOCRAPIClient._save_resources(FakeClient(), result, tmp_path)

    assert len(paths) == 4
    assert (tmp_path / "page-0001" / "markdown" / "imgs" / "chart.jpg").exists()
    assert (tmp_path / "page-0002" / "output" / "layout_det_res").exists()


def test_paddle_normalizer_preserves_page_bbox_formula_and_raw_pointer(tmp_path: Path) -> None:
    payload_path = tmp_path / "provider_response.json"
    resource_dir = tmp_path / "resources"
    image_path = resource_dir / "page-0001" / "markdown" / "imgs" / "figure.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")
    payload_path.write_text(
        json.dumps(
            {
                "job_id": "job-test",
                "pages": [
                    {
                        "pruned_result": {
                            "width": 1200,
                            "height": 1600,
                            "parsing_res_list": [
                                {
                                    "block_id": 1,
                                    "block_label": "doc_title",
                                    "block_order": 1,
                                    "block_bbox": [120, 160, 1080, 240],
                                    "block_content": "# A Paddle Paper",
                                },
                                {
                                    "block_id": 2,
                                    "block_label": "paragraph_title",
                                    "block_order": 2,
                                    "block_bbox": [120, 300, 600, 360],
                                    "block_content": "## 1 Method",
                                },
                                {
                                    "block_id": 3,
                                    "block_label": "display_formula",
                                    "block_order": 3,
                                    "block_bbox": [240, 400, 960, 500],
                                    "block_content": "$$ E = mc^2 $$",
                                },
                                {
                                    "block_id": 4,
                                    "block_label": "image",
                                    "block_order": None,
                                    "block_bbox": [120, 600, 1080, 1200],
                                    "block_content": '<img src="imgs/figure.jpg" /> architecture',
                                },
                            ],
                        }
                    }
                ],
                "data_info": {"numPages": 1},
            }
        ),
        encoding="utf-8",
    )
    source_pdf = tmp_path / "paper.pdf"
    source_pdf.touch()

    document = PaddleOCRNormalizer().normalize(
        payload_path,
        resource_dir=resource_dir,
        output_dir=tmp_path / "canonical",
        source_pdf=source_pdf,
        document_id="doc_paddle",
        parse_id="parse_paddle",
        source_sha256="b" * 64,
        source_filename="paper.pdf",
        provider_model="PaddleOCR-VL-1.6",
        page_ranges="3",
    )

    assert document.title == "A Paddle Paper"
    assert document.pages[0].page_number == 3
    assert document.blocks[0].source.bbox is not None
    assert document.blocks[0].source.bbox.x0 == 100
    assert document.blocks[0].source.bbox.y0 == 100
    assert document.blocks[2].latex == "E = mc^2"
    assert document.blocks[2].section_path == ["1 Method"]
    assert document.blocks[3].image_path == str(image_path.resolve())
    assert document.blocks[3].source.raw_json_pointer == (
        "/pages/0/pruned_result/parsing_res_list/3"
    )


def test_parser_gold_preparer_renders_and_does_not_overwrite_annotation(
    tmp_path: Path,
) -> None:
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    source = paper_dir / "sample.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Gold page")
        pdf.save(source)
    settings = Settings(data_dir=tmp_path / "data")
    source_hash = ParserGoldPreparer(settings).store.sha256(source)
    eval_dir = tmp_path / "evals"
    eval_dir.mkdir()
    (eval_dir / "schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["schema_version", "sample_id", "status"],
                "properties": {
                    "schema_version": {"const": "1.0"},
                    "sample_id": {"type": "string"},
                    "status": {"type": "string"},
                },
            }
        ),
        encoding="utf-8",
    )
    manifest = eval_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "sample_count": 1,
                "papers": [
                    {
                        "source_filename": source.name,
                        "source_sha256": source_hash,
                        "page_count": 1,
                        "samples": [
                            {
                                "sample_id": "gold_sample_p1",
                                "page_number": 1,
                                "tags": [],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    preparer = ParserGoldPreparer(settings)
    first = preparer.prepare(manifest, paper_dir=paper_dir)
    annotation = eval_dir / "annotations" / "gold_sample_p1.json"
    payload = json.loads(annotation.read_text(encoding="utf-8"))
    payload["notes"] = "human edit"
    annotation.write_text(json.dumps(payload), encoding="utf-8")
    second = preparer.prepare(manifest, paper_dir=paper_dir)

    assert first["sample_count"] == second["sample_count"] == 1
    assert Path(first["samples"][0]["page_image"]).is_file()
    assert json.loads(annotation.read_text(encoding="utf-8"))["notes"] == "human edit"
    assert preparer.audit(manifest)["counts"]["proposed"] == 1

    payload = json.loads(annotation.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": "reviewed",
            "elements": [
                {
                    "element_id": "e1",
                    "label": "paragraph",
                    "bbox": {
                        "x0": 10,
                        "y0": 10,
                        "x1": 900,
                        "y1": 120,
                        "coordinate_space": "normalized_0_1000",
                    },
                    "reading_order": 0,
                    "exclude_from_rag": False,
                }
            ],
            "annotation": {
                "annotator": "annotator-a",
                "annotated_at": "2026-08-01T00:00:00Z",
                "reviewer": "reviewer-b",
                "reviewed_at": "2026-08-01T01:00:00Z",
            },
        }
    )
    annotation.write_text(json.dumps(payload), encoding="utf-8")
    audit = preparer.audit(manifest)
    assert audit["gold_ready"] is True
    assert audit["counts"]["reviewed"] == 1

    payload["annotation"]["reviewer"] = "annotator-a"
    annotation.write_text(json.dumps(payload), encoding="utf-8")
    audit = preparer.audit(manifest)
    assert audit["gold_ready"] is False
    assert audit["failures"][0]["reason"] == "reviewer_must_differ_from_annotator"


def test_human_review_workbench_enforces_independence_and_computes_gates(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    evals_dir = tmp_path / "evals"
    settings = Settings(data_dir=data_dir, evals_dir=evals_dir)
    parser_root = evals_dir / "parser-gold"
    annotations = parser_root / "annotations"
    annotations.mkdir(parents=True)
    schema = json.loads(annotation_schema_path(parser_root).read_text(encoding="utf-8"))
    (parser_root / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (parser_root / "manifest.json").write_text(
        json.dumps(
            {
                "sample_count": 1,
                "papers": [
                    {
                        "source_filename": "paper.pdf",
                        "source_sha256": "a" * 64,
                        "samples": [{"sample_id": "gold_paper_p1", "page_number": 1, "tags": []}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    image = data_dir / "evaluations/parser-gold/pages/gold_paper_p1@2x.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"review page")
    image_hash = ArtifactStore.sha256(image)
    annotation = {
        "schema_version": "1.0",
        "sample_id": "gold_paper_p1",
        "source_sha256": "a" * 64,
        "source_filename": "paper.pdf",
        "page_number": 1,
        "page_image_sha256": image_hash,
        "status": "proposed",
        "tags": [],
        "notes": "",
        "elements": [],
        "qa_cases": [],
        "annotation": {},
    }
    (annotations / "gold_paper_p1.json").write_text(json.dumps(annotation), encoding="utf-8")
    document = make_high_quality_document()
    chunks = SemanticChunker().chunk(document)
    Catalog(settings).register(
        document,
        chunks,
        source_path=tmp_path / "source.pdf",
        artifact_dir=tmp_path / "artifact",
    )
    linked_chunk = chunks[0]
    qa_rows = [
        {
            "case_id": f"case_{index}",
            "question": f"Question {index}?",
            "reference": f"Reference {index}",
            "review_status": "candidate",
            "source_chunk_ids": [linked_chunk.id],
            "source_block_ids": [],
            "tags": [],
            "generator": {},
        }
        for index in range(30)
    ]
    (evals_dir / "initial-42-candidates.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in qa_rows), encoding="utf-8"
    )
    evaluation_dir = data_dir / "evaluations"
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    for index in range(30):
        score = index / 29
        (evaluation_dir / f"eval_{index}.json").write_text(
            json.dumps(
                {
                    "run_id": f"eval_{index}",
                    "created_at": "2026-08-01T00:00:00Z",
                    "case": {"question": f"Question {index}?"},
                    "answer": f"Answer {index}",
                    "metrics": {
                        "faithfulness": {"value": score},
                        "answer_relevancy": {"value": score},
                        "citation_validity": {"value": score},
                    },
                }
            ),
            encoding="utf-8",
        )

    service = HumanReviewService(settings)
    assert service.parser_image("gold_paper_p1") == image
    service.annotate_parser(
        "gold_paper_p1",
        actor="annotator-a",
        elements=[
            {
                "element_id": "e1",
                "label": "paragraph",
                "bbox": {
                    "x0": 10,
                    "y0": 10,
                    "x1": 900,
                    "y1": 120,
                    "coordinate_space": "normalized_0_1000",
                },
                "reading_order": 0,
                "exclude_from_rag": False,
            }
        ],
        qa_cases=[],
        notes="checked against the rendered page",
    )
    with pytest.raises(HumanReviewError, match="Reviewer must differ"):
        service.review_parser("gold_paper_p1", actor="annotator-a")
    service.review_parser("gold_paper_p1", actor="reviewer-b")
    assert service.parser_audit()["gold_ready"] is True

    for row in qa_rows:
        service.review_qa(
            row["case_id"],
            actor="qa-reviewer",
            decision="reviewed",
            question=row["question"],
            reference=row["reference"],
            tags=[],
            source_chunk_ids=row["source_chunk_ids"],
            source_block_ids=[],
            note="evidence checked",
        )
    assert service.qa_audit()["gold_ready"] is True

    for index in range(30):
        score = index / 29
        for rater, adjustment in (("rater-a", 0.0), ("rater-b", 0.01)):
            service.submit_rating(
                f"eval_{index}",
                rater=rater,
                faithfulness=min(1.0, score + adjustment),
                answer_relevancy=min(1.0, score + adjustment),
                citation_correctness=min(1.0, score + adjustment),
                notes="blind rubric score",
            )
    calibration = service.judge_calibration()
    assert calibration["gold_ready"] is True
    assert calibration["double_rated_runs"] == 30
    assert calibration["correlations"]["faithfulness"]["pearson"] > 0.99
    assert service.summary()["human_gate_ready"] is True
    assert len((service.audit_path).read_text(encoding="utf-8").splitlines()) == 92
    gate = service.write_gate_artifact()
    assert gate["human_gate_ready"] is True
    assert gate["source_sha256"]["audit_log"] == ArtifactStore.sha256(service.audit_path)
    assert Path(gate["artifact_path"]).is_file()


def test_backup_includes_data_and_human_eval_artifacts(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        evals_dir=tmp_path / "evals",
    )
    (settings.data_dir / "nested").mkdir(parents=True)
    (settings.evals_dir / "parser-gold/annotations").mkdir(parents=True)
    (settings.data_dir / "nested/run.json").write_text('{"run": 1}', encoding="utf-8")
    (settings.evals_dir / "parser-gold/annotations/sample.json").write_text(
        '{"status": "reviewed"}', encoding="utf-8"
    )
    archive = tmp_path / "backup.zip"

    created = BackupService(settings).create(archive)
    verified = BackupService.verify(archive)

    assert len(created["files"]) == 2
    assert {item["path"] for item in created["files"]} == {
        "data/nested/run.json",
        "evals/parser-gold/annotations/sample.json",
    }
    assert verified == {
        "valid": True,
        "file_count": 2,
        "failures": [],
        "created_at": created["created_at"],
    }


def test_rag_variant_metric_aggregation_ignores_failures() -> None:
    runs = [
        {
            "metrics": {
                "faithfulness": {"value": 1.0},
                "failed": {"value": None},
                "flag": {"value": True},
            }
        },
        {"metrics": {"faithfulness": {"value": 0.5}}},
    ]
    assert ParserBenchmarkRunner._aggregate_metrics(runs) == {"faithfulness": 0.75}
