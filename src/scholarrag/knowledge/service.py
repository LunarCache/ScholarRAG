from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from scholarrag.domain.models import CanonicalDocument


class PaperMetadata(BaseModel):
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    methods: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    section_titles: list[str] = Field(default_factory=list)
    extraction: dict[str, Any] = Field(default_factory=dict)


class MetadataExtractor:
    DATASET_PATTERN = re.compile(
        r"\b(ImageNet|CIFAR-?10|CIFAR-?100|COCO|WikiText|Criteo|MovieLens|"
        r"Amazon(?:-[A-Za-z]+)?|MIND|Gowalla|Yelp|KuaiRand|BookCrossing)\b",
        re.IGNORECASE,
    )
    METRIC_PATTERN = re.compile(
        r"\b(Recall@\d+|NDCG@\d+|MRR@\d*|Hit(?:Rate)?@\d+|AUC|Accuracy|"
        r"Precision|Recall|F1(?:-score)?|BLEU|ROUGE(?:-[12L])?)\b",
        re.IGNORECASE,
    )

    @classmethod
    def extract(cls, document: CanonicalDocument) -> PaperMetadata:
        text = "\n".join(block.text for block in document.blocks)
        front = text[:20_000]
        doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", front, re.IGNORECASE)
        arxiv_match = re.search(r"(?:arXiv\s*:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?", front, re.IGNORECASE)
        years = [int(value) for value in re.findall(r"\b(20[0-3]\d)\b", front[:5000])]
        headings = [
            block.text.strip()
            for block in document.blocks
            if block.kind.value in {"title", "heading"} and block.text.strip()
        ]
        method_candidates = []
        for heading in headings:
            cleaned = re.sub(r"^\d+(?:\.\d+)*\s*", "", heading).strip()
            if re.search(
                r"method|approach|model|architecture|framework|algorithm",
                cleaned,
                re.IGNORECASE,
            ):
                method_candidates.append(cleaned)
        return PaperMetadata(
            title=document.title,
            authors=document.authors,
            year=max(years) if years else None,
            doi=doi_match.group(0).rstrip(".,)") if doi_match else None,
            arxiv_id=arxiv_match.group(1) if arxiv_match else None,
            methods=cls._unique(method_candidates)[:20],
            datasets=cls._unique(match.group(0) for match in cls.DATASET_PATTERN.finditer(text)),
            metrics=cls._unique(match.group(0) for match in cls.METRIC_PATTERN.finditer(text)),
            section_titles=cls._unique(headings)[:100],
            extraction={
                "method": "deterministic_regex_and_canonical_headings",
                "source_parse_id": document.parse_id,
            },
        )

    @staticmethod
    def _unique(values: Any) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = str(value).strip()
            key = normalized.casefold()
            if normalized and key not in seen:
                output.append(normalized)
                seen.add(key)
        return output
