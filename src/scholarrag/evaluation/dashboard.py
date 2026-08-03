from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from scholarrag.config import Settings


class RegressionThresholds(BaseModel):
    metric_max_drop: dict[str, float] = Field(
        default_factory=lambda: {
            "faithfulness": 0.05,
            "answer_relevancy": 0.05,
            "retrieval_recall_at_k": 0.03,
            "citation_validity": 0.01,
        }
    )
    p95_latency_max_increase_ratio: float = 0.2


class QualityDashboard:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def snapshot(self) -> dict[str, Any]:
        query_runs = self._query_runs()
        evaluations = self._evaluations()
        parses = self._parses()
        stage_durations: dict[str, list[float]] = {}
        stage_failures: dict[str, int] = {}
        estimated_cost = 0.0
        for run in query_runs:
            for event in run.get("events", []):
                duration = event.get("duration_ms")
                status = event.get("status")
                stage_id = str(event.get("stage_id"))
                if status in {"failed", "cancelled"}:
                    stage_failures[stage_id] = stage_failures.get(stage_id, 0) + 1
                if isinstance(duration, int | float) and status not in {"failed", "cancelled"}:
                    stage_durations.setdefault(str(event.get("stage_id")), []).append(
                        float(duration)
                    )
                output = event.get("output") or {}
                cost = output.get("estimated_cost_cny")
                if isinstance(cost, int | float):
                    estimated_cost += float(cost)
        metric_values: dict[str, list[float]] = {}
        for evaluation in evaluations:
            for name, score in (evaluation.get("metrics") or {}).items():
                value = score.get("value") if isinstance(score, dict) else None
                if isinstance(value, int | float) and not isinstance(value, bool):
                    metric_values.setdefault(name, []).append(float(value))
        return {
            "schema_version": "1.0",
            "query_runs": {
                "count": len(query_runs),
                "status_counts": self._counts(run.get("status") for run in query_runs),
                "estimated_cost_cny": round(estimated_cost, 8),
            },
            "latency_ms": {
                stage: self._distribution(values) for stage, values in stage_durations.items()
            },
            "stage_failure_counts": stage_failures,
            "evaluation": {
                "run_count": len(evaluations),
                "metric_means": {
                    name: round(statistics.fmean(values), 6)
                    for name, values in metric_values.items()
                },
                "metric_samples": {name: len(values) for name, values in metric_values.items()},
            },
            "parsing": {
                "parse_count": len(parses),
                "decision_counts": self._counts(
                    (item.get("quality") or {}).get("decision") for item in parses
                ),
                "mean_quality_score": self._mean(
                    (item.get("quality") or {}).get("score") for item in parses
                ),
            },
            "versions": {
                "embedding_model": self.settings.unified_embedding_model,
                "multimodal_embedding_model": self.settings.multimodal_embedding_model,
                "embedding_dimensions": self.settings.embedding_dimensions,
                "rerank_model": self.settings.rerank_model,
                "chat_model": self.settings.chat_model,
                "chat_enable_thinking": self.settings.chat_enable_thinking,
                "judge_model": self.settings.ragas_judge_model,
                "judge_enable_thinking": self.settings.ragas_judge_enable_thinking,
            },
        }

    @staticmethod
    def compare(
        baseline: dict[str, Any],
        candidate: dict[str, Any],
        thresholds: RegressionThresholds | None = None,
    ) -> dict[str, Any]:
        thresholds = thresholds or RegressionThresholds()
        baseline_metrics = baseline.get("evaluation", {}).get("metric_means", {})
        candidate_metrics = candidate.get("evaluation", {}).get("metric_means", {})
        failures: list[dict[str, Any]] = []
        for metric, max_drop in thresholds.metric_max_drop.items():
            before = baseline_metrics.get(metric)
            after = candidate_metrics.get(metric)
            if isinstance(before, int | float) and isinstance(after, int | float):
                drop = float(before) - float(after)
                if drop > max_drop:
                    failures.append(
                        {
                            "kind": "metric_drop",
                            "metric": metric,
                            "baseline": before,
                            "candidate": after,
                            "drop": drop,
                            "max_drop": max_drop,
                        }
                    )
        baseline_latency = baseline.get("latency_ms", {})
        candidate_latency = candidate.get("latency_ms", {})
        for stage, before_values in baseline_latency.items():
            before = before_values.get("p95")
            after = candidate_latency.get(stage, {}).get("p95")
            if (
                isinstance(before, int | float)
                and before > 0
                and isinstance(after, int | float)
                and after / before - 1 > thresholds.p95_latency_max_increase_ratio
            ):
                failures.append(
                    {
                        "kind": "latency_regression",
                        "stage": stage,
                        "baseline_p95": before,
                        "candidate_p95": after,
                    }
                )
        return {"passed": not failures, "failures": failures}

    def _query_runs(self) -> list[dict[str, Any]]:
        output = []
        for path in self.settings.data_dir.glob("query_runs/run_*/run.json"):
            try:
                run = json.loads(path.read_text(encoding="utf-8"))
                events_path = path.with_name("events.jsonl")
                run["events"] = (
                    [
                        json.loads(line)
                        for line in events_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    if events_path.is_file()
                    else []
                )
                output.append(run)
            except (OSError, ValueError):
                continue
        return output

    def _evaluations(self) -> list[dict[str, Any]]:
        return self._json_objects(self.settings.data_dir / "evaluations", "eval_*.json")

    def _parses(self) -> list[dict[str, Any]]:
        return self._json_objects(self.settings.artifacts_dir, "*/parses/*/manifest.json")

    @staticmethod
    def _json_objects(root: Path, pattern: str) -> list[dict[str, Any]]:
        output = []
        for path in root.glob(pattern):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    output.append(value)
            except (OSError, ValueError):
                continue
        return output

    @staticmethod
    def _distribution(values: list[float]) -> dict[str, float | int]:
        ordered = sorted(values)
        p95_index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
        return {
            "count": len(ordered),
            "p50": round(statistics.median(ordered), 3),
            "p95": round(ordered[p95_index], 3),
            "mean": round(statistics.fmean(ordered), 3),
        }

    @staticmethod
    def _counts(values: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            key = str(value or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _mean(values: Any) -> float | None:
        numeric = [float(value) for value in values if isinstance(value, int | float)]
        return round(statistics.fmean(numeric), 6) if numeric else None
