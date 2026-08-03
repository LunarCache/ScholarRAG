from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from scholarrag.domain.models import BlockKind, CanonicalDocument
from scholarrag.ingestion.quality import ParseQualityReport


class ParseComparisonReport(BaseModel):
    schema_version: str = "1.0"
    primary_parse_id: str
    fallback_parse_id: str
    compared_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    page_count_match: bool
    shared_page_count: int
    page_text_similarity: float = Field(ge=0, le=1)
    content_volume_similarity: float = Field(ge=0, le=1)
    block_type_similarity: float = Field(ge=0, le=1)
    visual_count_similarity: float = Field(ge=0, le=1)
    overall_similarity: float = Field(ge=0, le=1)
    primary_type_counts: dict[str, int]
    fallback_type_counts: dict[str, int]
    disagreement_flags: list[str] = Field(default_factory=list)


class ParseRouteDecision(BaseModel):
    schema_version: str = "1.0"
    route_id: str = Field(default_factory=lambda: f"route_{uuid.uuid4().hex}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    primary_parse_id: str
    fallback_parse_id: str | None = None
    recommended_action: Literal[
        "activate_primary", "activate_fallback", "manual_review", "run_fallback"
    ]
    selected_parse_id: str | None = None
    reason: str
    primary_quality_score: float
    primary_quality_decision: Literal["accept", "review", "fallback"]
    fallback_quality_score: float | None = None
    fallback_quality_decision: Literal["accept", "review", "fallback"] | None = None
    comparison: ParseComparisonReport | None = None


class ParseRouter:
    """Compare provider-neutral canonical parses and make an auditable route decision."""

    def __init__(self, *, min_score_improvement: float = 0.03) -> None:
        if not 0 <= min_score_improvement <= 1:
            raise ValueError("min_score_improvement must be between 0 and 1")
        self.min_score_improvement = min_score_improvement

    @staticmethod
    def should_run_fallback(
        mode: Literal["off", "on_fallback", "on_review"],
        quality: ParseQualityReport,
    ) -> bool:
        if mode == "off" or quality.decision == "accept":
            return False
        if mode == "on_fallback":
            return quality.decision == "fallback"
        return quality.decision in {"review", "fallback"}

    def route(
        self,
        primary: ParseQualityReport,
        fallback: ParseQualityReport | None = None,
        *,
        primary_document: CanonicalDocument | None = None,
        fallback_document: CanonicalDocument | None = None,
    ) -> ParseRouteDecision:
        if fallback is None:
            if primary.decision == "accept":
                action: Literal[
                    "activate_primary", "activate_fallback", "manual_review", "run_fallback"
                ] = "activate_primary"
                selected = primary.parse_id
                reason = "Primary parse passed the quality gate."
            else:
                action = "run_fallback"
                selected = None
                reason = f"Primary parse requires {primary.decision}; no fallback was evaluated."
            return ParseRouteDecision(
                primary_parse_id=primary.parse_id,
                recommended_action=action,
                selected_parse_id=selected,
                reason=reason,
                primary_quality_score=primary.score,
                primary_quality_decision=primary.decision,
            )

        comparison = None
        if primary_document is not None and fallback_document is not None:
            comparison = self.compare(primary_document, fallback_document)
        tier = {"fallback": 0, "review": 1, "accept": 2}
        primary_tier = tier[primary.decision]
        fallback_tier = tier[fallback.decision]
        if primary_tier < 2 and fallback_tier < 2:
            action = "manual_review"
            selected = None
            reason = "Neither parser passed the quality gate."
        elif fallback_tier > primary_tier:
            action = "activate_fallback"
            selected = fallback.parse_id
            reason = "Fallback parse has a stronger quality-gate decision."
        elif primary_tier > fallback_tier:
            action = "activate_primary"
            selected = primary.parse_id
            reason = "Primary parse has a stronger quality-gate decision."
        elif comparison and comparison.disagreement_flags:
            action = "manual_review"
            selected = None
            reason = "Parsers disagree on critical content dimensions: " + ", ".join(
                comparison.disagreement_flags
            )
        elif fallback.score >= primary.score + self.min_score_improvement:
            action = "activate_fallback"
            selected = fallback.parse_id
            reason = (
                f"Fallback quality score improves by {fallback.score - primary.score:.4f}, "
                f"above the {self.min_score_improvement:.4f} threshold."
            )
        else:
            action = "activate_primary"
            selected = primary.parse_id
            reason = "Fallback does not provide a material deterministic quality improvement."
        return ParseRouteDecision(
            primary_parse_id=primary.parse_id,
            fallback_parse_id=fallback.parse_id,
            recommended_action=action,
            selected_parse_id=selected,
            reason=reason,
            primary_quality_score=primary.score,
            primary_quality_decision=primary.decision,
            fallback_quality_score=fallback.score,
            fallback_quality_decision=fallback.decision,
            comparison=comparison,
        )

    @classmethod
    def compare(
        cls,
        primary: CanonicalDocument,
        fallback: CanonicalDocument,
    ) -> ParseComparisonReport:
        primary_pages = cls._page_tokens(primary)
        fallback_pages = cls._page_tokens(fallback)
        shared_pages = sorted(set(primary_pages).intersection(fallback_pages))
        page_similarities = [
            cls._jaccard(primary_pages[page], fallback_pages[page]) for page in shared_pages
        ]
        page_text_similarity = sum(page_similarities) / max(1, len(page_similarities))
        primary_chars = sum(len(block.text.strip()) for block in primary.blocks)
        fallback_chars = sum(len(block.text.strip()) for block in fallback.blocks)
        content_volume_similarity = cls._count_similarity(primary_chars, fallback_chars)
        primary_types = Counter(block.kind.value for block in primary.blocks)
        fallback_types = Counter(block.kind.value for block in fallback.blocks)
        block_type_similarity = cls._distribution_similarity(primary_types, fallback_types)
        primary_visuals = sum(
            primary_types[kind.value]
            for kind in (BlockKind.FIGURE, BlockKind.TABLE, BlockKind.FORMULA)
        )
        fallback_visuals = sum(
            fallback_types[kind.value]
            for kind in (BlockKind.FIGURE, BlockKind.TABLE, BlockKind.FORMULA)
        )
        visual_count_similarity = cls._count_similarity(primary_visuals, fallback_visuals)
        page_count_match = set(primary_pages) == set(fallback_pages)
        overall = (
            0.50 * page_text_similarity
            + 0.20 * content_volume_similarity
            + 0.20 * block_type_similarity
            + 0.10 * visual_count_similarity
        )
        flags: list[str] = []
        if not page_count_match:
            flags.append("page_set_mismatch")
        if page_text_similarity < 0.55:
            flags.append("low_page_text_similarity")
        if content_volume_similarity < 0.60:
            flags.append("content_volume_mismatch")
        for kind in (BlockKind.FORMULA, BlockKind.TABLE, BlockKind.FIGURE):
            if cls._count_similarity(primary_types[kind.value], fallback_types[kind.value]) < 0.50:
                flags.append(f"{kind.value}_count_mismatch")
        return ParseComparisonReport(
            primary_parse_id=primary.parse_id,
            fallback_parse_id=fallback.parse_id,
            page_count_match=page_count_match,
            shared_page_count=len(shared_pages),
            page_text_similarity=round(page_text_similarity, 4),
            content_volume_similarity=round(content_volume_similarity, 4),
            block_type_similarity=round(block_type_similarity, 4),
            visual_count_similarity=round(visual_count_similarity, 4),
            overall_similarity=round(overall, 4),
            primary_type_counts=dict(sorted(primary_types.items())),
            fallback_type_counts=dict(sorted(fallback_types.items())),
            disagreement_flags=flags,
        )

    @staticmethod
    def _page_tokens(document: CanonicalDocument) -> dict[int, set[str]]:
        pages: dict[int, set[str]] = {}
        for block in document.blocks:
            tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", block.text.casefold())
            pages.setdefault(block.source.page_number, set()).update(tokens)
        for page in document.pages:
            pages.setdefault(page.page_number, set())
        return pages

    @staticmethod
    def _jaccard(left: set[str], right: set[str]) -> float:
        if not left and not right:
            return 1.0
        return len(left.intersection(right)) / max(1, len(left.union(right)))

    @staticmethod
    def _count_similarity(left: int, right: int) -> float:
        if left == right == 0:
            return 1.0
        return min(left, right) / max(left, right)

    @staticmethod
    def _distribution_similarity(left: Counter[str], right: Counter[str]) -> float:
        left_total = sum(left.values()) or 1
        right_total = sum(right.values()) or 1
        keys = set(left).union(right)
        distance = sum(abs(left[key] / left_total - right[key] / right_total) for key in keys)
        return max(0.0, 1.0 - distance / 2)
