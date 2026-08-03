from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, Field

from scholarrag.domain.models import BlockKind, CanonicalDocument
from scholarrag.ingestion.heading_hierarchy import (
    infer_explicit_heading_level,
    is_caption_heading,
)


class QualitySignal(BaseModel):
    code: str
    score: float = Field(ge=0, le=1)
    weight: float = Field(ge=0, le=1)
    value: Any = None
    threshold: Any = None
    passed: bool
    severity: Literal["info", "warning", "critical"]
    message: str


class ParseQualityReport(BaseModel):
    schema_version: str = "1.0"
    document_id: str
    parse_id: str
    provider: str
    provider_model: str
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expected_page_count: int | None
    parsed_page_count: int
    score: float = Field(ge=0, le=1)
    decision: Literal["accept", "review", "fallback"]
    recommended_action: Literal["activate", "manual_review", "run_fallback"]
    signals: list[QualitySignal]
    critical_failures: list[str] = Field(default_factory=list)
    thresholds: dict[str, float]


class ParseQualityGate:
    """Deterministic, provider-neutral checks over the canonical document."""

    def __init__(self, *, accept_score: float = 0.85, review_score: float = 0.65) -> None:
        if not 0 <= review_score <= accept_score <= 1:
            raise ValueError("quality thresholds must satisfy 0 <= review <= accept <= 1")
        self.accept_score = accept_score
        self.review_score = review_score

    def evaluate(
        self,
        document: CanonicalDocument,
        *,
        expected_page_count: int | None = None,
    ) -> ParseQualityReport:
        pages = sorted({page.page_number for page in document.pages})
        blocks = document.blocks
        page_chars: Counter[int] = Counter()
        page_visuals: Counter[int] = Counter()
        for block in blocks:
            page_chars[block.source.page_number] += len(block.text.strip())
            if block.kind in {BlockKind.FIGURE, BlockKind.TABLE, BlockKind.FORMULA}:
                page_visuals[block.source.page_number] += 1

        page_denominator = expected_page_count or len(pages) or 1
        page_coverage = min(1.0, len(pages) / page_denominator)
        expected_pages = set(range(1, page_denominator + 1))
        missing_pages = sorted(expected_pages.difference(pages)) if expected_page_count else []
        content_pages = sum(
            page_chars[page_number] >= 80 or page_visuals[page_number] > 0 for page_number in pages
        )
        content_page_ratio = content_pages / max(1, len(pages))
        chars_per_page = sum(page_chars.values()) / max(1, len(pages))
        text_density_score = min(1.0, chars_per_page / 1_200)

        substantive = [
            block
            for block in blocks
            if block.kind not in {BlockKind.HEADER, BlockKind.FOOTER, BlockKind.PAGE_NUMBER}
        ]
        bbox_coverage = sum(block.source.bbox is not None for block in substantive) / max(
            1, len(substantive)
        )
        known_kind_ratio = sum(block.kind != BlockKind.UNKNOWN for block in blocks) / max(
            1, len(blocks)
        )
        raw_pointer_coverage = sum(
            bool(block.source.raw_artifact and block.source.raw_json_pointer) for block in blocks
        ) / max(1, len(blocks))

        page_block_ids = [block_id for page in document.pages for block_id in page.block_ids]
        document_block_ids = [block.id for block in blocks]
        linkage_score = (
            1.0
            if len(page_block_ids) == len(document_block_ids)
            and set(page_block_ids) == set(document_block_ids)
            else 0.0
        )
        reading_orders = [block.reading_order for block in blocks]
        reading_order_score = (
            1.0
            if len(reading_orders) == len(set(reading_orders))
            and reading_orders == sorted(reading_orders)
            else 0.0
        )

        all_text = "".join(block.text for block in blocks)
        bad_chars = len(re.findall(r"[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]", all_text))
        bad_char_ratio = bad_chars / max(1, len(all_text))
        clean_text_score = max(0.0, 1.0 - bad_char_ratio / 0.01)

        duplicate_candidates = [
            re.sub(r"\s+", " ", block.text.strip()).casefold()
            for block in substantive
            if len(block.text.strip()) >= 20
        ]
        duplicate_count = len(duplicate_candidates) - len(set(duplicate_candidates))
        duplicate_ratio = duplicate_count / max(1, len(duplicate_candidates))
        uniqueness_score = max(0.0, 1.0 - duplicate_ratio / 0.25)
        headings = [block for block in blocks if block.kind == BlockKind.HEADING]
        heading_levels = [
            int(block.metadata["heading_level"])
            for block in headings
            if isinstance(block.metadata.get("heading_level"), int)
        ]
        heading_jumps = sum(
            current > previous + 1 for previous, current in pairwise(heading_levels)
        )
        numbered_level_mismatches = sum(
            inferred is not None and inferred != block.metadata.get("heading_level")
            for block in headings
            if (inferred := infer_explicit_heading_level(block.text)) is not None
        )
        path_depth_mismatches = sum(
            isinstance(block.metadata.get("heading_level"), int)
            and len(block.section_path) != block.metadata["heading_level"]
            for block in headings
        )
        caption_headings = sum(is_caption_heading(block.text) for block in headings)
        level_counts = Counter(heading_levels)
        explicit_depths = {
            level
            for block in headings
            if (level := infer_explicit_heading_level(block.text)) is not None
        }
        degenerate_levels = bool(
            len(heading_levels) >= 8
            and level_counts
            and max(level_counts.values()) / len(heading_levels) >= 0.9
            and len(explicit_depths) >= 2
        )
        hierarchy_defects = (
            heading_jumps
            + numbered_level_mismatches
            + path_depth_mismatches
            + caption_headings
            + int(degenerate_levels)
        )
        heading_hierarchy_score = 1.0 - min(1.0, hierarchy_defects / max(1, len(headings)))
        formulas = [block for block in blocks if block.kind == BlockKind.FORMULA]
        formula_representation = sum(
            bool(block.latex or block.markdown) for block in formulas
        ) / max(1, len(formulas))
        tables = [block for block in blocks if block.kind == BlockKind.TABLE]
        table_representation = sum(
            bool(block.html or block.markdown or block.text) for block in tables
        ) / max(1, len(tables))
        figures = [block for block in blocks if block.kind == BlockKind.FIGURE]
        figure_caption_coverage = sum(
            bool(block.text.strip() or block.metadata.get("caption_text")) for block in figures
        ) / max(1, len(figures))

        signals = [
            self._signal(
                "page_coverage",
                page_coverage,
                0.25,
                value=round(page_coverage, 4),
                threshold=1.0,
                passed=page_coverage >= 0.999,
                critical=bool(missing_pages) or not pages,
                message=(
                    f"Parsed {len(pages)} of {page_denominator} expected pages"
                    + (f"; missing {missing_pages[:20]}" if missing_pages else "")
                ),
            ),
            self._signal(
                "content_pages",
                content_page_ratio,
                0.15,
                value=round(content_page_ratio, 4),
                threshold=0.95,
                passed=content_page_ratio >= 0.95,
                critical=content_page_ratio < 0.5,
                message=f"{content_pages}/{len(pages)} parsed pages contain usable content",
            ),
            self._signal(
                "text_density",
                text_density_score,
                0.10,
                value=round(chars_per_page, 1),
                threshold=1_200,
                passed=chars_per_page >= 1_200,
                message="Average extracted text characters per parsed page",
            ),
            self._signal(
                "bbox_coverage",
                bbox_coverage,
                0.10,
                value=round(bbox_coverage, 4),
                threshold=0.95,
                passed=bbox_coverage >= 0.95,
                message="Substantive blocks retaining page coordinates",
            ),
            self._signal(
                "known_block_types",
                known_kind_ratio,
                0.05,
                value=round(known_kind_ratio, 4),
                threshold=0.9,
                passed=known_kind_ratio >= 0.9,
                message="Blocks mapped to a known canonical content type",
            ),
            self._signal(
                "raw_pointer_coverage",
                raw_pointer_coverage,
                0.15,
                value=round(raw_pointer_coverage, 4),
                threshold=0.98,
                passed=raw_pointer_coverage >= 0.98,
                critical=raw_pointer_coverage < 0.5,
                message="Blocks traceable to a raw artifact and JSON pointer",
            ),
            self._signal(
                "page_block_linkage",
                linkage_score,
                0.05,
                value=bool(linkage_score),
                threshold=True,
                passed=bool(linkage_score),
                critical=not bool(linkage_score),
                message="Page block lists and document blocks form a lossless bijection",
            ),
            self._signal(
                "reading_order",
                reading_order_score,
                0.05,
                value=bool(reading_order_score),
                threshold=True,
                passed=bool(reading_order_score),
                message="Canonical reading_order values are unique and monotonic",
            ),
            self._signal(
                "clean_text",
                clean_text_score,
                0.05,
                value=round(bad_char_ratio, 6),
                threshold=0.001,
                passed=bad_char_ratio <= 0.001,
                message="Replacement and invalid control-character ratio",
            ),
            self._signal(
                "content_uniqueness",
                uniqueness_score,
                0.05,
                value=round(duplicate_ratio, 4),
                threshold=0.15,
                passed=duplicate_ratio <= 0.15,
                message="Exact duplicate ratio among substantive text blocks",
            ),
            self._signal(
                "heading_hierarchy",
                heading_hierarchy_score,
                0.0,
                value={
                    "heading_count": len(headings),
                    "level_jumps": heading_jumps,
                    "numbered_level_mismatches": numbered_level_mismatches,
                    "path_depth_mismatches": path_depth_mismatches,
                    "caption_headings": caption_headings,
                    "level_distribution": dict(sorted(level_counts.items())),
                    "degenerate_levels": degenerate_levels,
                },
                threshold=0.95,
                passed=heading_hierarchy_score >= 0.95,
                message="Heading numbering, paths and provider levels form a coherent hierarchy",
            ),
            self._signal(
                "formula_representation",
                formula_representation,
                0.0,
                value={"formula_count": len(formulas), "coverage": formula_representation},
                threshold=0.9,
                passed=not formulas or formula_representation >= 0.9,
                message="Formula blocks retain LaTeX or Markdown representation",
            ),
            self._signal(
                "table_representation",
                table_representation,
                0.0,
                value={"table_count": len(tables), "coverage": table_representation},
                threshold=0.9,
                passed=not tables or table_representation >= 0.9,
                message="Table blocks retain HTML, Markdown, or text representation",
            ),
            self._signal(
                "figure_caption_coverage",
                figure_caption_coverage,
                0.0,
                value={"figure_count": len(figures), "coverage": figure_caption_coverage},
                threshold=0.8,
                passed=not figures or figure_caption_coverage >= 0.8,
                message="Figure blocks retain a caption for cross-modal association",
            ),
        ]
        score = round(sum(signal.score * signal.weight for signal in signals), 4)
        critical_failures = [signal.code for signal in signals if signal.severity == "critical"]
        if critical_failures or score < self.review_score:
            decision: Literal["accept", "review", "fallback"] = "fallback"
            recommended_action: Literal["activate", "manual_review", "run_fallback"] = (
                "run_fallback"
            )
        elif score < self.accept_score or any(not signal.passed for signal in signals):
            decision = "review"
            recommended_action = "manual_review"
        else:
            decision = "accept"
            recommended_action = "activate"
        return ParseQualityReport(
            document_id=document.document_id,
            parse_id=document.parse_id,
            provider=document.provider,
            provider_model=document.provider_model,
            expected_page_count=expected_page_count,
            parsed_page_count=len(pages),
            score=score,
            decision=decision,
            recommended_action=recommended_action,
            signals=signals,
            critical_failures=critical_failures,
            thresholds={"accept_score": self.accept_score, "review_score": self.review_score},
        )

    @staticmethod
    def _signal(
        code: str,
        score: float,
        weight: float,
        *,
        value: Any,
        threshold: Any,
        passed: bool,
        message: str,
        critical: bool = False,
    ) -> QualitySignal:
        return QualitySignal(
            code=code,
            score=max(0.0, min(1.0, score)),
            weight=weight,
            value=value,
            threshold=threshold,
            passed=passed,
            severity="critical" if critical else ("info" if passed else "warning"),
            message=message,
        )
