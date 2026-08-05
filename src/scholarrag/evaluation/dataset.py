from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Literal

import pymupdf

from scholarrag.config import Settings
from scholarrag.evaluation.service import EvaluationCase
from scholarrag.providers.siliconflow import SiliconFlowClient, SiliconFlowError
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog


class EasyDatasetContextExporter:
    @staticmethod
    def export(settings: Settings, target: Path) -> dict[str, Any]:
        catalog = Catalog(settings)
        rows: list[dict[str, Any]] = []
        for document in catalog.list_documents():
            parse_dir = Path(document["artifact_dir"])
            chunks_path = parse_dir / "chunks" / "chunks.json"
            if not chunks_path.is_file():
                continue
            for chunk in json.loads(chunks_path.read_text(encoding="utf-8")):
                if chunk.get("level") == "parent":
                    continue
                rows.append(
                    {
                        "id": chunk["id"],
                        "content": chunk["contextual_text"],
                        "metadata": {
                            "document_id": chunk["document_id"],
                            "parse_id": chunk["parse_id"],
                            "page_numbers": chunk["page_numbers"],
                            "block_ids": chunk["block_ids"],
                            "section_path": chunk.get("section_path", []),
                        },
                    }
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
            encoding="utf-8",
        )
        report = {"exported": len(rows), "target": str(target.resolve())}
        ArtifactStore.write_json(target.with_suffix(target.suffix + ".export.json"), report)
        return report


class EvaluationCandidateGenerator:
    """Generate grounded QA candidates in paper-sized batches; never auto-promote to gold."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def generate_from_parser_manifest(
        self,
        manifest_path: Path,
        *,
        paper_dir: Path,
        target: Path,
        review_status: Literal["candidate", "synthetic"] = "candidate",
    ) -> dict[str, Any]:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        provider = SiliconFlowClient(self.settings)
        cases = self._load_existing(target)
        completed_samples = {
            str(case.generator.get("sample_id"))
            for case in cases
            if case.generator.get("sample_id")
        }
        rejected: list[dict[str, str]] = []
        try:
            jobs: list[tuple[dict[str, Any], dict[str, str], dict[str, Any]]] = []
            for paper in manifest["papers"]:
                remaining = [
                    sample
                    for sample in paper["samples"]
                    if sample["sample_id"] not in completed_samples
                ]
                if not remaining:
                    continue
                source = paper_dir / paper["source_filename"]
                page_text: dict[str, str] = {}
                with pymupdf.open(source) as pdf:
                    for sample in paper["samples"]:
                        text = pdf.load_page(sample["page_number"] - 1).get_text("text")
                        page_text[sample["sample_id"]] = self._clean_text(text)[:12_000]
                for sample_to_generate in remaining:
                    jobs.append((paper, page_text, sample_to_generate))
            tasks = [
                asyncio.create_task(
                    self._generate_one(
                        provider,
                        paper=paper,
                        page_text=page_text,
                        sample=sample,
                        manifest_path=manifest_path,
                        review_status=review_status,
                    )
                )
                for paper, page_text, sample in jobs
            ]
            for task in asyncio.as_completed(tasks):
                case, failure = await task
                if case is not None:
                    cases.append(case)
                    completed_samples.add(str(case.generator["sample_id"]))
                    self._write_cases(target, cases)
                elif failure is not None:
                    rejected.append(failure)
        finally:
            await provider.close()
        self._write_cases(target, cases)
        report = {
            "generated": len(cases),
            "rejected": rejected,
            "review_status": review_status,
            "generation_config": {
                "model": self.settings.chat_model,
                "temperature": self.settings.chat_temperature,
                "top_p": self.settings.chat_top_p,
                "top_k": self.settings.chat_top_k,
                "enable_thinking": self.settings.chat_enable_thinking,
                "response_format": "json_object",
                "output_token_limit": None,
                "max_concurrency": self.settings.provider_max_concurrency,
            },
            "source_manifest": str(manifest_path.resolve()),
            "target": str(target.resolve()),
        }
        ArtifactStore.write_json(target.with_suffix(target.suffix + ".generation.json"), report)
        return report

    async def _generate_one(
        self,
        provider: SiliconFlowClient,
        *,
        paper: dict[str, Any],
        page_text: dict[str, str],
        sample: dict[str, Any],
        manifest_path: Path,
        review_status: Literal["candidate", "synthetic"],
    ) -> tuple[EvaluationCase | None, dict[str, str] | None]:
        sample_id = str(sample["sample_id"])
        try:
            response = await provider.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You build rigorous academic RAG evaluation data. Return JSON "
                            "only. Never use knowledge absent from the supplied page text."
                        ),
                    },
                    {"role": "user", "content": self._prompt(paper, page_text, [sample])},
                ],
                temperature=self.settings.chat_temperature,
                top_p=self.settings.chat_top_p,
                top_k=self.settings.chat_top_k,
                response_format={"type": "json_object"},
            )
            payload = self._json_payload(response.data["choices"][0]["message"]["content"])
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            return None, {
                "paper": str(paper["source_filename"]),
                "row": sample_id,
                "reason": f"invalid_json:{type(exc).__name__}",
            }
        except SiliconFlowError as exc:
            return None, {
                "paper": str(paper["source_filename"]),
                "row": sample_id,
                "reason": f"provider_error:{type(exc).__name__}",
            }
        rows = payload.get("items")
        if not isinstance(rows, list):
            return None, {
                "paper": str(paper["source_filename"]),
                "row": sample_id,
                "reason": "missing_items",
            }
        for row in rows:
            if not isinstance(row, dict) or str(row.get("sample_id") or "") != sample_id:
                continue
            source_text = page_text[sample_id]
            quote = self._clean_text(str(row.get("evidence_quote") or ""))
            question = str(row.get("question") or "").strip()
            reference = str(row.get("reference") or "").strip()
            if len(quote) > 300:
                return None, {
                    "paper": str(paper["source_filename"]),
                    "row": sample_id,
                    "reason": "evidence_quote_too_long",
                }
            if len(reference) > 600:
                return None, {
                    "paper": str(paper["source_filename"]),
                    "row": sample_id,
                    "reason": "reference_too_long",
                }
            if not quote or quote.casefold() not in source_text.casefold():
                return None, {
                    "paper": str(paper["source_filename"]),
                    "row": sample_id,
                    "reason": "evidence_quote_not_verbatim",
                }
            if not question or not reference:
                return None, {
                    "paper": str(paper["source_filename"]),
                    "row": sample_id,
                    "reason": "missing_question_or_reference",
                }
            return (
                EvaluationCase(
                    question=question,
                    document_ids=[f"doc_{paper['source_sha256'][:24]}"],
                    reference=reference,
                    tags=list(dict.fromkeys([*sample["tags"], *row.get("tags", [])])),
                    review_status=review_status,
                    generator={
                        "kind": "siliconflow_page_grounded",
                        "model": self.settings.chat_model,
                        "temperature": self.settings.chat_temperature,
                        "top_p": self.settings.chat_top_p,
                        "top_k": self.settings.chat_top_k,
                        "enable_thinking": self.settings.chat_enable_thinking,
                        "response_format": "json_object",
                        "output_token_limit": None,
                        "sample_id": sample_id,
                        "page_number": sample["page_number"],
                        "evidence_quote": quote,
                        "provider_trace_id": response.trace_id,
                        "source_manifest": str(manifest_path),
                    },
                ),
                None,
            )
        return None, {
            "paper": str(paper["source_filename"]),
            "row": sample_id,
            "reason": "sample_id_not_returned",
        }

    @staticmethod
    def _prompt(
        paper: dict[str, Any],
        page_text: dict[str, str],
        batch: list[dict[str, Any]],
    ) -> str:
        samples = [
            {
                "sample_id": sample["sample_id"],
                "page_number": sample["page_number"],
                "tags": sample["tags"],
                "page_text": page_text[sample["sample_id"]],
            }
            for sample in batch
        ]
        return (
            f"Paper: {paper['source_filename']}\n"
            "Create exactly one useful QA item for every supplied sample. Cover formulas, tables, "
            "figures, methods, experiments, limitations, references, and unanswerable scope as the "
            "sample permits. Each answer must be directly supported. evidence_quote must be a "
            "verbatim contiguous substring from page_text and at most 300 characters. Keep each "
            'reference below 600 characters. Return {"items":[{"sample_id":...,'
            '"question":...,"reference":...,"evidence_quote":...,"tags":[...]}]}.\n\n'
            + json.dumps(samples, ensure_ascii=False)
        )

    @staticmethod
    def _clean_text(value: str) -> str:
        return " ".join(value.split())

    @staticmethod
    def _json_payload(value: str) -> dict[str, Any]:
        cleaned = value.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            cleaned = cleaned.rsplit("```", 1)[0]
        payload = json.loads(cleaned)
        if not isinstance(payload, dict):
            raise TypeError("Expected a JSON object")
        return payload

    @staticmethod
    def _write_cases(target: Path, cases: list[EvaluationCase]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "\n".join(case.model_dump_json() for case in cases) + ("\n" if cases else ""),
            encoding="utf-8",
        )

    @staticmethod
    def _load_existing(target: Path) -> list[EvaluationCase]:
        if not target.is_file():
            return []
        return [
            EvaluationCase.model_validate_json(line)
            for line in target.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class EvaluationDatasetLinker:
    @staticmethod
    def link(settings: Settings, dataset: Path) -> dict[str, Any]:
        catalog = Catalog(settings)
        cases = [
            EvaluationCase.model_validate_json(line)
            for line in dataset.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        linked = 0
        unresolved: list[str] = []
        for case in cases:
            document_ids = case.document_ids or []
            quote = EvaluationCandidateGenerator._clean_text(
                str(case.generator.get("evidence_quote") or "")
            )
            page_number = case.generator.get("page_number")
            matches: list[dict[str, Any]] = []
            adjacent_exact_matches: list[dict[str, Any]] = []
            adjacent_candidates: list[tuple[float, dict[str, Any]]] = []
            page_candidates: list[tuple[float, dict[str, Any]]] = []
            for document_id in document_ids:
                document = catalog.get_document(document_id)
                if not document:
                    continue
                chunks_path = Path(document["artifact_dir"]) / "chunks" / "chunks.json"
                for chunk in json.loads(chunks_path.read_text(encoding="utf-8")):
                    if chunk.get("level") == "parent":
                        continue
                    chunk_text = EvaluationCandidateGenerator._clean_text(chunk["text"])
                    page_match = (
                        not isinstance(page_number, int) or page_number in chunk["page_numbers"]
                    )
                    exact_match = bool(quote and quote.casefold() in chunk_text.casefold())
                    adjacent_page_match = isinstance(page_number, int) and any(
                        abs(int(candidate_page) - page_number) == 1
                        for candidate_page in chunk["page_numbers"]
                    )
                    if exact_match and page_match:
                        matches.append(chunk)
                    elif exact_match and adjacent_page_match:
                        adjacent_exact_matches.append(chunk)
                    elif quote and adjacent_page_match:
                        adjacent_candidates.append(
                            (EvaluationDatasetLinker._token_recall(quote, chunk_text), chunk)
                        )
                    elif quote and page_match:
                        page_candidates.append(
                            (EvaluationDatasetLinker._token_recall(quote, chunk_text), chunk)
                        )
            link_method = "exact_normalized_substring"
            link_score = 1.0
            if not matches and adjacent_exact_matches:
                matches = adjacent_exact_matches
                link_method = "adjacent_page_exact"
            if not matches and adjacent_candidates:
                link_score, best = max(adjacent_candidates, key=lambda item: item[0])
                if link_score >= 0.9:
                    matches = [best]
                    link_method = "adjacent_page_high_recall"
            if not matches and page_candidates:
                link_score, best = max(page_candidates, key=lambda item: item[0])
                if link_score >= 0.55:
                    matches = [best]
                    link_method = "page_scoped_token_recall"
            if matches:
                case.source_chunk_ids = list(dict.fromkeys(row["id"] for row in matches))
                case.source_block_ids = list(
                    dict.fromkeys(block_id for row in matches for block_id in row["block_ids"])
                )
                case.generator["evidence_link_method"] = link_method
                case.generator["evidence_link_score"] = round(link_score, 4)
                linked += 1
            else:
                unresolved.append(case.case_id)
        dataset.write_text(
            "\n".join(case.model_dump_json() for case in cases) + ("\n" if cases else ""),
            encoding="utf-8",
        )
        report = {
            "case_count": len(cases),
            "linked": linked,
            "unresolved_case_ids": unresolved,
            "dataset": str(dataset.resolve()),
        }
        ArtifactStore.write_json(dataset.with_suffix(dataset.suffix + ".link.json"), report)
        return report

    @staticmethod
    def _token_recall(reference: str, candidate: str) -> float:
        reference_tokens = set(
            re.findall(r"[A-Za-z0-9_.%-]+|[\u4e00-\u9fff]", reference.casefold())
        )
        candidate_tokens = set(
            re.findall(r"[A-Za-z0-9_.%-]+|[\u4e00-\u9fff]", candidate.casefold())
        )
        return len(reference_tokens & candidate_tokens) / max(1, len(reference_tokens))
