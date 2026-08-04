"""Metrics for the visual evidence loop: usage tracking and ablation.

Quantifies how often the vision LLM actually received images (usage) and
compares answer quality between text-only and text-plus-image runs (ablation).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VisualUsage:
    """Visual counters for a single query run."""

    included: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.included + self.skipped


@dataclass(frozen=True)
class VisualUsageSummary:
    """Aggregated visual usage across query runs."""

    run_count: int
    runs_with_visuals: int
    total_included: int
    total_skipped: int
    inclusion_rate: float
    skip_rate: float


@dataclass(frozen=True)
class AblationComparison:
    """Mean score delta for one metric between text-only and text+image runs."""

    metric: str
    baseline_mean: float
    visual_mean: float
    delta: float


def extract_visual_usage(events: list[dict[str, Any]]) -> VisualUsage:
    """Extract visual counters from stage-complete events.

    Events carry ``visual_images_included`` / ``visual_images_skipped`` in
    their output (generation stages). The last stage that set them wins.
    """
    for event in reversed(events):
        output = event.get("output") or {}
        if "visual_images_included" in output or "visual_images_skipped" in output:
            return VisualUsage(
                included=int(output.get("visual_images_included") or 0),
                skipped=int(output.get("visual_images_skipped") or 0),
            )
    return VisualUsage()


def summarize_visual_usage(usages: list[VisualUsage]) -> VisualUsageSummary:
    """Aggregate per-run visual usage into summary rates."""
    run_count = len(usages)
    runs_with_visuals = sum(1 for usage in usages if usage.included > 0)
    total_included = sum(usage.included for usage in usages)
    total_skipped = sum(usage.skipped for usage in usages)
    total = total_included + total_skipped
    return VisualUsageSummary(
        run_count=run_count,
        runs_with_visuals=runs_with_visuals,
        total_included=total_included,
        total_skipped=total_skipped,
        inclusion_rate=runs_with_visuals / run_count if run_count else 0.0,
        skip_rate=total_skipped / total if total else 0.0,
    )


def compare_ablation(
    baseline: list[float], visual: list[float], *, metric: str
) -> AblationComparison:
    """Compare mean scores of one metric across two run groups."""
    baseline_mean = statistics.mean(baseline) if baseline else 0.0
    visual_mean = statistics.mean(visual) if visual else 0.0
    return AblationComparison(
        metric=metric,
        baseline_mean=baseline_mean,
        visual_mean=visual_mean,
        delta=visual_mean - baseline_mean,
    )
