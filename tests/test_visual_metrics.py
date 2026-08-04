"""Phase 4: visual evidence metrics — usage tracking and ablation."""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarrag.config import Settings
from scholarrag.evaluation.service import RagasEvaluator
from scholarrag.evaluation.visual_metrics import (
    AblationComparison,
    VisualUsage,
    compare_ablation,
    extract_visual_usage,
    summarize_visual_usage,
)
from scholarrag.rag.runs import QueryRunStore

# ---------------------------------------------------------------------------
# extract_visual_usage
# ---------------------------------------------------------------------------


def test_extract_usage_from_stage_complete_events() -> None:
    events = [
        {"stage_id": "retrieval.query", "output": {}},
        {
            "stage_id": "generation.answer",
            "output": {"answer_chars": 42, "visual_images_included": 2, "visual_images_skipped": 1},
        },
    ]

    usage = extract_visual_usage(events)

    assert usage.included == 2
    assert usage.skipped == 1
    assert usage.total == 3


def test_extract_usage_last_stage_wins() -> None:
    events = [
        {
            "stage_id": "generation.answer",
            "output": {"visual_images_included": 1, "visual_images_skipped": 0},
        },
        {
            "stage_id": "agent.synthesize",
            "output": {"visual_images_included": 4, "visual_images_skipped": 2},
        },
    ]

    usage = extract_visual_usage(events)

    assert usage.included == 4
    assert usage.skipped == 2


def test_extract_usage_empty_events() -> None:
    assert extract_visual_usage([]) == VisualUsage()
    assert extract_visual_usage([{"stage_id": "x", "output": {}}]) == VisualUsage()


def test_extract_usage_missing_fields_default_zero() -> None:
    events = [{"stage_id": "generation.answer", "output": {"visual_images_included": 3}}]

    usage = extract_visual_usage(events)

    assert usage.included == 3
    assert usage.skipped == 0


# ---------------------------------------------------------------------------
# summarize_visual_usage
# ---------------------------------------------------------------------------


def test_summary_computes_rates() -> None:
    usages = [
        VisualUsage(included=2, skipped=0),
        VisualUsage(included=1, skipped=1),
        VisualUsage(included=0, skipped=0),
    ]

    summary = summarize_visual_usage(usages)

    assert summary.run_count == 3
    assert summary.runs_with_visuals == 2
    assert summary.total_included == 3
    assert summary.total_skipped == 1
    assert summary.inclusion_rate == pytest.approx(2 / 3)
    assert summary.skip_rate == pytest.approx(1 / 4)


def test_summary_empty_list() -> None:
    summary = summarize_visual_usage([])

    assert summary.run_count == 0
    assert summary.inclusion_rate == 0.0
    assert summary.skip_rate == 0.0


# ---------------------------------------------------------------------------
# compare_ablation
# ---------------------------------------------------------------------------


def test_compare_ablation_computes_delta() -> None:
    comparison = compare_ablation([0.5, 0.6], [0.8, 0.9, 1.0], metric="answer_relevancy")

    assert isinstance(comparison, AblationComparison)
    assert comparison.metric == "answer_relevancy"
    assert comparison.baseline_mean == pytest.approx(0.55)
    assert comparison.visual_mean == pytest.approx(0.9)
    assert comparison.delta == pytest.approx(0.35)


def test_compare_ablation_empty_groups() -> None:
    comparison = compare_ablation([], [], metric="m")

    assert comparison.baseline_mean == 0.0
    assert comparison.visual_mean == 0.0
    assert comparison.delta == 0.0


# ---------------------------------------------------------------------------
# RagasEvaluator._visual_metrics reads query run events
# ---------------------------------------------------------------------------


def test_visual_metrics_reads_query_run_events(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", phoenix_endpoint=None)
    store = QueryRunStore(settings)
    run = _create_run(store, question="表格数据是什么？")
    _append_generation_event(store, run.run_id, included=2, skipped=0)

    metrics = RagasEvaluator(settings)._visual_metrics(run.run_id)

    assert metrics["visual_images_included"].value == 2
    assert metrics["visual_images_skipped"].value == 0


def test_visual_metrics_missing_run_returns_empty(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", phoenix_endpoint=None)

    assert RagasEvaluator(settings)._visual_metrics("no-such-run") == {}
    assert RagasEvaluator(settings)._visual_metrics(None) == {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _create_run(store: QueryRunStore, *, question: str) -> object:
    import asyncio

    async def _create() -> object:
        return await store.create(question=question, document_ids=None, top_k=4, agentic=False)

    return asyncio.run(_create())


def _append_generation_event(
    store: QueryRunStore, run_id: str, *, included: int, skipped: int
) -> None:
    import asyncio

    async def _append() -> None:
        await store.append_event(
            run_id,
            event_type="stage.complete",
            stage_id="generation.answer",
            label="大模型生成回答",
            status="success",
            component="siliconflow",
            model="test-model",
            summary="生成回答",
            output={
                "answer_chars": 10,
                "visual_images_included": included,
                "visual_images_skipped": skipped,
            },
        )

    asyncio.run(_append())


def test_query_run_store_isolation(tmp_path: Path) -> None:
    """Sanity check: two stores in different data dirs do not collide."""
    store_a = QueryRunStore(Settings(data_dir=tmp_path / "a", phoenix_endpoint=None))
    store_b = QueryRunStore(Settings(data_dir=tmp_path / "b", phoenix_endpoint=None))
    run = _create_run(store_a, question="q")

    assert store_b.get(run.run_id) is None
    assert store_a.get(run.run_id) is not None
