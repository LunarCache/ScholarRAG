from __future__ import annotations

import json
from pathlib import Path

from scholarrag.domain.models import BlockKind
from scholarrag.ingestion.heading_hierarchy import (
    classify_heading,
    infer_explicit_heading_level,
)
from scholarrag.ingestion.mineru_normalizer import MinerUNormalizer
from scholarrag.ingestion.quality import ParseQualityGate


def test_explicit_heading_inference_and_caption_guard() -> None:
    assert infer_explicit_heading_level("3 METHODS") == 1
    assert infer_explicit_heading_level("3.2 METHODS") == 2
    assert infer_explicit_heading_level("3.2.1 DETAILS") == 3
    assert infer_explicit_heading_level("A ADDITIONAL RESULTS") == 1
    assert infer_explicit_heading_level("A.1 GPT-3 EXPERIMENTS") == 2
    assert infer_explicit_heading_level("ABSTRACT") == 1
    assert classify_heading("Table 8: Prompt results", 2).kind == BlockKind.CAPTION


def test_mineru_repairs_degenerate_heading_levels(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    headings = [
        ("A Traceable Paper", 1),
        ("ABSTRACT", 2),
        ("1 INTRODUCTION", 2),
        ("3 KNOWLEDGE-INTENSIVE REASONING TASKS", 2),
        ("3.2 METHODS", 2),
        ("Table 1: Prompting results", 2),
        ("A ADDITIONAL RESULTS", 2),
        ("A.1 GPT-3 EXPERIMENTS", 2),
    ]
    entries = [
        {
            "type": "title",
            "content": {
                "title_content": [{"type": "text", "content": text}],
                "level": level,
            },
            "bbox": [100, index * 80, 900, index * 80 + 40],
        }
        for index, (text, level) in enumerate(headings)
    ]
    entries.append(
        {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {"type": "text", "content": "Grounded evidence remains traceable."}
                ]
            },
            "bbox": [100, 700, 900, 760],
        }
    )
    (raw_dir / "fixture_content_list_v2.json").write_text(json.dumps([entries]), encoding="utf-8")
    (raw_dir / "layout.json").write_text(
        json.dumps({"pdf_info": [{"page_idx": 0, "page_size": [612, 792]}]}),
        encoding="utf-8",
    )

    document = MinerUNormalizer().normalize(
        raw_dir,
        document_id="doc_headings",
        parse_id="parse_headings",
        source_sha256="a" * 64,
        source_filename="headings.pdf",
        provider_model="vlm",
    )
    by_text = {block.text: block for block in document.blocks}

    assert by_text["ABSTRACT"].section_path == ["ABSTRACT"]
    assert by_text["1 INTRODUCTION"].section_path == ["1 INTRODUCTION"]
    assert by_text["3.2 METHODS"].section_path == [
        "3 KNOWLEDGE-INTENSIVE REASONING TASKS",
        "3.2 METHODS",
    ]
    assert by_text["A.1 GPT-3 EXPERIMENTS"].section_path == [
        "A ADDITIONAL RESULTS",
        "A.1 GPT-3 EXPERIMENTS",
    ]
    caption = by_text["Table 1: Prompting results"]
    assert caption.kind == BlockKind.CAPTION
    assert caption.section_path == [
        "3 KNOWLEDGE-INTENSIVE REASONING TASKS",
        "3.2 METHODS",
    ]
    assert caption.metadata["provider_heading_level"] == 2
    assert caption.metadata["heading_level_source"] == "caption_guard"

    report = ParseQualityGate().evaluate(document, expected_page_count=1)
    hierarchy = next(signal for signal in report.signals if signal.code == "heading_hierarchy")
    assert hierarchy.passed
    assert hierarchy.value["numbered_level_mismatches"] == 0
    assert hierarchy.value["caption_headings"] == 0


def test_quality_gate_rejects_unrepaired_degenerate_levels(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    titles = ["A Paper", "ABSTRACT", "1 INTRODUCTION", "2 METHOD", "2.1 SETUP"]
    titles.extend(f"{index} SECTION" for index in range(3, 10))
    entries = [
        {
            "type": "title",
            "content": {
                "title_content": [{"type": "text", "content": text}],
                "level": 1 if index == 0 else 2,
            },
            "bbox": [100, index * 50, 900, index * 50 + 30],
        }
        for index, text in enumerate(titles)
    ]
    (raw_dir / "fixture_content_list_v2.json").write_text(json.dumps([entries]), encoding="utf-8")
    (raw_dir / "layout.json").write_text(
        json.dumps({"pdf_info": [{"page_idx": 0, "page_size": [612, 792]}]}),
        encoding="utf-8",
    )
    document = MinerUNormalizer().normalize(
        raw_dir,
        document_id="doc_broken",
        parse_id="parse_broken",
        source_sha256="b" * 64,
        source_filename="broken.pdf",
        provider_model="vlm",
    )
    broken = document.model_copy(deep=True)
    for block in broken.blocks:
        if block.kind != BlockKind.HEADING:
            continue
        block.metadata["heading_level"] = 2
        block.section_path = ["ABSTRACT", block.text]

    report = ParseQualityGate().evaluate(broken, expected_page_count=1)
    hierarchy = next(signal for signal in report.signals if signal.code == "heading_hierarchy")
    assert not hierarchy.passed
    assert hierarchy.value["numbered_level_mismatches"] > 0
    assert report.decision == "review"
