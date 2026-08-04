"""Route visual evidence usage based on parse quality reports.

OCR text is the carrier for locating and citing content, but when the
quality report signals that extracted text is unreliable (formulas/tables
without representation, low text density, dirty text), the vision LLM must
read the figures directly. This module turns a parse quality report into a
visual evidence policy: whether images are forced on even when the cost
switch is disabled. There is no image-count budget — every distinct
figure/table image among the retrieved chunks is attached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scholarrag.ingestion.quality import ParseQualityReport
from scholarrag.storage.catalog import Catalog

# Signals whose failure means the extracted text alone is unreliable and the
# model should read the original figures/tables directly.
_FORCE_SIGNALS: dict[str, str] = {
    "formula_representation": "公式块缺少 LaTeX/Markdown 表示",
    "table_representation": "表格块缺少结构化表示",
    "text_density": "文本密度过低",
    "clean_text": "文本含损坏字符",
    "figure_caption_coverage": "图块缺少说明文字",
    "content_uniqueness": "文本存在重复内容",
}


@dataclass(frozen=True)
class VisualRoutingDecision:
    """Visual evidence policy derived from one or more quality reports."""

    force_visual: bool = False
    reasons: list[str] = field(default_factory=list)


def visual_routing_policy(report: ParseQualityReport) -> VisualRoutingDecision:
    """Derive a visual evidence policy from a single parse quality report."""
    by_code = {signal.code: signal for signal in report.signals}
    reasons: list[str] = []
    force = False

    for code, message in _FORCE_SIGNALS.items():
        signal = by_code.get(code)
        if signal is not None and not signal.passed:
            force = True
            reasons.append(f"{message}（{code}）")

    if report.decision in {"review", "fallback"}:
        reasons.append(f"解析质量为 {report.decision}，建议依赖视觉证据")

    if not reasons:
        reasons.append("解析质量良好，按默认策略使用视觉证据")
    return VisualRoutingDecision(force_visual=force, reasons=reasons)


def merge_routing_decisions(
    decisions: list[VisualRoutingDecision],
) -> VisualRoutingDecision:
    """Merge per-document policies: force if any forces."""
    if not decisions:
        return VisualRoutingDecision()
    return VisualRoutingDecision(
        force_visual=any(decision.force_visual for decision in decisions),
        reasons=[reason for decision in decisions for reason in decision.reasons],
    )


def load_quality_reports(catalog: Catalog, document_ids: set[str]) -> list[ParseQualityReport]:
    """Load persisted quality reports for documents from the catalog.

    Documents without a report (or with an unreadable one) are skipped so
    routing degrades to the default policy.
    """
    reports: list[ParseQualityReport] = []
    for document_id in sorted(document_ids):
        document = catalog.get_document(document_id)
        if not document:
            continue
        report_path = Path(document["artifact_dir"]) / "quality" / "report.json"
        try:
            payload: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
            reports.append(ParseQualityReport.model_validate(payload))
        except (OSError, ValueError):
            continue
    return reports
