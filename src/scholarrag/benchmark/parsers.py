from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, ClassVar

from scholarrag.config import Settings
from scholarrag.domain.models import CanonicalDocument, Chunk
from scholarrag.evaluation.service import RagasEvaluator, load_cases
from scholarrag.ingestion.chunker import SemanticChunker
from scholarrag.ingestion.paddleocr_normalizer import PaddleOCRNormalizer
from scholarrag.providers.paddleocr import PaddleOCRAPIClient
from scholarrag.retrieval.index import VectorIndex, VisualIndex
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog


class ParserBenchmarkError(RuntimeError):
    pass


class ParserBenchmarkRunner:
    """Freeze raw parser outputs and produce comparable, non-judgmental statistics."""

    PADDLE_VARIANTS: ClassVar[dict[str, str]] = {
        "paddleocr-vl-1.6": "PaddleOCR-VL-1.6",
        "pp-structure-v3": "PP-StructureV3",
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = ArtifactStore(settings)
        self.root = settings.data_dir / "benchmarks" / "parsers"

    async def run(
        self,
        source: Path,
        *,
        page_ranges: str | None = None,
        mineru_parse_dir: Path | None = None,
    ) -> Path:
        source = source.resolve()
        source_hash = self.store.sha256(source)
        document_id = f"doc_{source_hash[:24]}"
        run_id = f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
        run_dir = self.root / run_id
        variants_dir = run_dir / "variants"
        variants_dir.mkdir(parents=True, exist_ok=False)

        manifest: dict[str, Any] = {
            "schema_version": "1.0",
            "run_id": run_id,
            "status": "running",
            "source": str(source),
            "source_filename": source.name,
            "source_sha256": source_hash,
            "document_id": document_id,
            "page_ranges": page_ranges,
            "created_at": datetime.now(UTC).isoformat(),
            "protocol": "docs/PARSER_BENCHMARK.md",
            "variants": [],
        }
        self.store.write_json(run_dir / "manifest.json", manifest)

        results: list[dict[str, Any]] = []
        try:
            mineru_dir = mineru_parse_dir or self._latest_mineru_parse(document_id)
            mineru_stats = self._measure_mineru(mineru_dir, page_ranges=page_ranges)
            mineru_target = variants_dir / "mineru-vlm"
            mineru_target.mkdir()
            self.store.write_json(mineru_target / "stats.json", mineru_stats)
            results.append(mineru_stats)

            for slug, model in self.PADDLE_VARIANTS.items():
                results.append(
                    await self._run_paddle(
                        source,
                        variants_dir / slug,
                        slug=slug,
                        model=model,
                        page_ranges=page_ranges,
                    )
                )

            report = {
                "run_id": run_id,
                "source_sha256": source_hash,
                "page_ranges": page_ranges,
                "warning": (
                    "These are artifact/structure statistics, not accuracy scores. "
                    "Accuracy requires the gold annotations defined in docs/PARSER_BENCHMARK.md."
                ),
                "variants": results,
            }
            self.store.write_json(run_dir / "report.json", report)
            (run_dir / "report.md").write_text(self._markdown_report(report), encoding="utf-8")
            manifest.update(
                {
                    "status": "complete",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "variants": [item["variant"] for item in results],
                    "report": str(run_dir / "report.json"),
                }
            )
            self.store.write_json(run_dir / "manifest.json", manifest)
            return run_dir
        except Exception as exc:
            manifest.update(
                {
                    "status": "failed",
                    "failed_at": datetime.now(UTC).isoformat(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "variants": [item["variant"] for item in results],
                }
            )
            self.store.write_json(run_dir / "manifest.json", manifest)
            raise

    async def resume(self, run_dir: Path) -> Path:
        """Resume a failed benchmark from frozen provider responses without resubmitting them."""
        run_dir = run_dir.resolve()
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            raise ParserBenchmarkError(f"Missing benchmark manifest: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = Path(manifest["source"])
        if self.store.sha256(source) != manifest["source_sha256"]:
            raise ParserBenchmarkError("Source PDF hash changed; refusing to resume")
        previous_failure = {
            key: manifest.get(key)
            for key in ("failed_at", "error_type", "error")
            if manifest.get(key) is not None
        }
        manifest.update(
            {
                "status": "running",
                "resumed_at": datetime.now(UTC).isoformat(),
                "previous_failures": [*manifest.get("previous_failures", []), previous_failure],
            }
        )
        for key in ("failed_at", "error_type", "error"):
            manifest.pop(key, None)
        self.store.write_json(manifest_path, manifest)

        variants_dir = run_dir / "variants"
        mineru_stats_path = variants_dir / "mineru-vlm" / "stats.json"
        results = [json.loads(mineru_stats_path.read_text(encoding="utf-8"))]
        try:
            for slug, model in self.PADDLE_VARIANTS.items():
                variant_dir = variants_dir / slug
                payload_path = variant_dir / "raw" / "provider_response.json"
                if payload_path.exists():
                    results.append(
                        await self._reuse_paddle_payload(
                            payload_path,
                            variant_dir,
                            slug=slug,
                            model=model,
                            page_ranges=manifest.get("page_ranges"),
                        )
                    )
                else:
                    results.append(
                        await self._run_paddle(
                            source,
                            variant_dir,
                            slug=slug,
                            model=model,
                            page_ranges=manifest.get("page_ranges"),
                        )
                    )
            return self._complete_run(run_dir, manifest, results)
        except Exception as exc:
            manifest.update(
                {
                    "status": "failed",
                    "failed_at": datetime.now(UTC).isoformat(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "variants": [item["variant"] for item in results],
                }
            )
            self.store.write_json(manifest_path, manifest)
            raise

    def normalize_paddle_variants(self, run_dir: Path) -> list[dict[str, Any]]:
        """Create comparable canonical artifacts without registering or indexing a parse."""
        run_dir = run_dir.resolve()
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            raise ParserBenchmarkError(f"Missing benchmark manifest: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = Path(manifest["source"]).resolve()
        source_hash = self.store.sha256(source)
        if source_hash != manifest["source_sha256"]:
            raise ParserBenchmarkError("Source PDF hash changed; refusing to normalize")

        outputs: list[dict[str, Any]] = []
        for slug, default_model in self.PADDLE_VARIANTS.items():
            variant_dir = run_dir / "variants" / slug
            payload_path = variant_dir / "raw" / "provider_response.json"
            submission_path = variant_dir / "raw" / "submission.json"
            if not payload_path.exists():
                raise ParserBenchmarkError(f"Missing frozen response: {payload_path}")
            submission = (
                json.loads(submission_path.read_text(encoding="utf-8"))
                if submission_path.exists()
                else {}
            )
            model = submission.get("model") or default_model
            page_ranges = submission.get("page_ranges") or manifest.get("page_ranges")
            parse_id = f"paddleocr-{slug}-{manifest['run_id']}"
            canonical_dir = variant_dir / "canonical"
            document = PaddleOCRNormalizer().normalize(
                payload_path,
                resource_dir=variant_dir / "resources",
                output_dir=canonical_dir,
                source_pdf=source,
                document_id=manifest["document_id"],
                parse_id=parse_id,
                source_sha256=source_hash,
                source_filename=source.name,
                provider_model=model,
                page_ranges=page_ranges,
            )
            chunks = SemanticChunker().chunk(document)
            document_path = canonical_dir / "document.json"
            chunks_path = variant_dir / "chunks" / "chunks.json"
            self.store.write_json(document_path, document.model_dump(mode="json"))
            self.store.write_json(chunks_path, [chunk.model_dump(mode="json") for chunk in chunks])
            result = {
                "variant": slug,
                "model": model,
                "parse_id": parse_id,
                "page_count": len(document.pages),
                "block_count": len(document.blocks),
                "chunk_count": len(chunks),
                "visual_block_count": sum(
                    block.kind.value in {"figure", "table"} and block.image_path is not None
                    for block in document.blocks
                ),
                "document_artifact": str(document_path),
                "chunks_artifact": str(chunks_path),
                "normalized_at": datetime.now(UTC).isoformat(),
                "registered": False,
                "indexed": False,
            }
            self.store.write_json(variant_dir / "normalization.json", result)
            outputs.append(result)

        manifest["normalizations"] = outputs
        manifest["normalized_at"] = datetime.now(UTC).isoformat()
        self.store.write_json(manifest_path, manifest)
        return outputs

    async def benchmark_rag_variants(self, run_dir: Path, dataset: Path) -> dict[str, Any]:
        """Run isolated indexes and Ragas so parser variants cannot contaminate one another."""
        run_dir = run_dir.resolve()
        dataset = dataset.resolve()
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        source = Path(manifest["source"]).resolve()
        if not manifest.get("normalizations"):
            self.normalize_paddle_variants(run_dir)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        variants = self._rag_variant_artifacts(run_dir)
        cases = load_cases(dataset)
        comparison_root = run_dir / "rag-evaluation"
        comparison_root.mkdir(parents=True, exist_ok=True)
        outputs: list[dict[str, Any]] = []

        for variant, document_path, chunks_path, artifact_dir in variants:
            document = CanonicalDocument.model_validate_json(
                document_path.read_text(encoding="utf-8")
            )
            chunks = [
                Chunk.model_validate(item)
                for item in json.loads(chunks_path.read_text(encoding="utf-8"))
            ]
            variant_root = comparison_root / variant
            variant_settings = self.settings.model_copy(
                update={"data_dir": variant_root, "phoenix_endpoint": None}
            )
            variant_settings.ensure_directories()
            Catalog(variant_settings).register(
                document, chunks, source_path=source, artifact_dir=artifact_dir
            )
            index_manifest_path = variant_root / "index_manifest.json"
            fingerprint = self._chunk_fingerprint(document, chunks)
            index_manifest = (
                json.loads(index_manifest_path.read_text(encoding="utf-8"))
                if index_manifest_path.exists()
                else {}
            )
            vector_index = VectorIndex(variant_settings)
            visual_index = VisualIndex(variant_settings)
            reusable = (
                index_manifest.get("status") == "ready"
                and index_manifest.get("fingerprint") == fingerprint
                and index_manifest.get("embedding_model")
                == variant_settings.unified_embedding_model
                and index_manifest.get("embedding_dimensions")
                == variant_settings.embedding_dimensions
                and index_manifest.get("visual_embedding_model")
                == variant_settings.multimodal_embedding_model
                and await vector_index.exists()
            )
            if reusable:
                indexed_count = int(index_manifest.get("indexed_count", 0))
                visual_indexed_count = int(index_manifest.get("visual_indexed_count", 0))
            else:
                indexed_count = await vector_index.index(chunks)
                visual_indexed_count = await visual_index.index(document, chunks)
                index_manifest = {
                    "status": "ready",
                    "variant": variant,
                    "parse_id": document.parse_id,
                    "fingerprint": fingerprint,
                    "embedding_model": variant_settings.unified_embedding_model,
                    "embedding_dimensions": variant_settings.embedding_dimensions,
                    "visual_embedding_model": variant_settings.multimodal_embedding_model,
                    "indexed_count": indexed_count,
                    "visual_indexed_count": visual_indexed_count,
                    "completed_at": datetime.now(UTC).isoformat(),
                }
                self.store.write_json(index_manifest_path, index_manifest)

            evaluator = RagasEvaluator(variant_settings)
            runs = []
            for case in cases:
                run = await evaluator.evaluate_rag(case)
                runs.append(
                    {
                        "run_id": run.run_id,
                        "question": run.case.question,
                        "answer": run.answer,
                        "answer_trace_id": run.answer_trace_id,
                        "retrieved_chunk_ids": run.retrieved_chunk_ids,
                        "retrieved_context_count": len(run.retrieved_contexts),
                        "retrieved_context_chars": sum(
                            len(context) for context in run.retrieved_contexts
                        ),
                        "metrics": {
                            name: score.model_dump(mode="json")
                            for name, score in run.metrics.items()
                        },
                        "artifact_path": run.artifact_path,
                    }
                )
            outputs.append(
                {
                    "variant": variant,
                    "provider": document.provider,
                    "model": document.provider_model,
                    "parse_id": document.parse_id,
                    "chunk_count": len(chunks),
                    "indexed_count": indexed_count,
                    "visual_indexed_count": visual_indexed_count,
                    "index_reused": reusable,
                    "runs": runs,
                    "aggregate_metrics": self._aggregate_metrics(runs),
                }
            )

        report = {
            "schema_version": "1.0",
            "benchmark_run_id": manifest["run_id"],
            "dataset": str(dataset),
            "dataset_case_count": len(cases),
            "warning": "Smoke comparison only; parser ranking requires reviewed gold QA cases.",
            "embedding_model": self.settings.unified_embedding_model,
            "embedding_dimensions": self.settings.embedding_dimensions,
            "visual_embedding_model": self.settings.multimodal_embedding_model,
            "rerank_model": self.settings.rerank_model,
            "chat_model": self.settings.chat_model,
            "chat_enable_thinking": self.settings.chat_enable_thinking,
            "judge_model": self.settings.ragas_judge_model,
            "judge_context_window_tokens": (self.settings.ragas_judge_context_window_tokens),
            "judge_output_limit": None,
            "variants": outputs,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        report_path = comparison_root / "report.json"
        self.store.write_json(report_path, report)
        report["report_path"] = str(report_path)
        return report

    @staticmethod
    def _rag_variant_artifacts(
        run_dir: Path,
    ) -> list[tuple[str, Path, Path, Path]]:
        mineru_stats = json.loads(
            (run_dir / "variants" / "mineru-vlm" / "stats.json").read_text(encoding="utf-8")
        )
        mineru_dir = Path(mineru_stats["source_parse_dir"]).resolve()
        variants = [
            (
                "mineru-vlm",
                mineru_dir / "canonical" / "document.json",
                mineru_dir / "chunks" / "chunks.json",
                mineru_dir,
            )
        ]
        for slug in ParserBenchmarkRunner.PADDLE_VARIANTS:
            variant_dir = run_dir / "variants" / slug
            variants.append(
                (
                    slug,
                    variant_dir / "canonical" / "document.json",
                    variant_dir / "chunks" / "chunks.json",
                    variant_dir,
                )
            )
        for variant, document, chunks, _ in variants:
            if not document.exists() or not chunks.exists():
                raise ParserBenchmarkError(f"Missing canonical artifacts for {variant}")
        return variants

    @staticmethod
    def _chunk_fingerprint(document: CanonicalDocument, chunks: list[Chunk]) -> str:
        payload = {
            "parse_id": document.parse_id,
            "chunks": [
                {"id": chunk.id, "contextual_text": chunk.contextual_text} for chunk in chunks
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _aggregate_metrics(runs: list[dict[str, Any]]) -> dict[str, float]:
        values: dict[str, list[float]] = {}
        for run in runs:
            for name, score in run["metrics"].items():
                value = score.get("value")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values.setdefault(name, []).append(float(value))
        return {
            name: sum(metric_values) / len(metric_values)
            for name, metric_values in sorted(values.items())
            if metric_values
        }

    def _latest_mineru_parse(self, document_id: str) -> Path:
        parse_root = self.store.document_dir(document_id) / "parses"
        candidates = []
        for path in parse_root.glob("mineru-*"):
            manifest_path = path / "manifest.json"
            if not manifest_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") == "ready":
                candidates.append(path)
        if not candidates:
            raise ParserBenchmarkError(
                f"No completed MinerU parse for {document_id}; ingest it first or pass --mineru-parse-dir"
            )
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _measure_mineru(self, parse_dir: Path, *, page_ranges: str | None) -> dict[str, Any]:
        parse_dir = parse_dir.resolve()
        document_path = parse_dir / "canonical" / "document.json"
        manifest_path = parse_dir / "manifest.json"
        if not document_path.exists() or not manifest_path.exists():
            raise ParserBenchmarkError(f"Invalid MinerU parse directory: {parse_dir}")
        document = CanonicalDocument.model_validate_json(document_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        selected = self._page_numbers(page_ranges, total=len(document.pages))
        blocks = [block for block in document.blocks if block.source.page_number in selected]
        type_counts = Counter(block.kind.value for block in blocks)
        text_chars = sum(len(block.text) for block in blocks)
        bbox_count = sum(block.source.bbox is not None for block in blocks)
        files, byte_count = self._tree_size(parse_dir / "raw" / "mineru")
        return {
            "variant": "mineru-vlm",
            "provider": "mineru",
            "model": manifest.get("provider_model", "vlm"),
            "provider_job_id": manifest.get("batch_id"),
            "source_parse_dir": str(parse_dir),
            "page_ranges": page_ranges,
            "returned_pages": len(selected),
            "blocks": len(blocks),
            "type_counts": dict(sorted(type_counts.items())),
            "text_chars": text_chars,
            "markdown_chars": self._mineru_markdown_chars(parse_dir),
            "bbox_blocks": bbox_count,
            "bbox_coverage": round(bbox_count / len(blocks), 4) if blocks else 0.0,
            "raw_artifact_files": files,
            "raw_artifact_bytes": byte_count,
            "latency_seconds": self._manifest_latency(manifest),
            "options": {"model_version": manifest.get("provider_model", "vlm")},
            "measured_at": datetime.now(UTC).isoformat(),
        }

    async def _run_paddle(
        self,
        source: Path,
        variant_dir: Path,
        *,
        slug: str,
        model: str,
        page_ranges: str | None,
    ) -> dict[str, Any]:
        raw_dir = variant_dir / "raw"
        resources_dir = variant_dir / "resources"
        raw_dir.mkdir(parents=True)
        resources_dir.mkdir(parents=True)
        options, options_payload = self._paddle_options(model)
        started_at = datetime.now(UTC)
        start = time.perf_counter()
        client = PaddleOCRAPIClient(self.settings)
        job = await client.submit_document(
            source,
            model=model,
            options=options,
            page_ranges=page_ranges,
        )
        submission = {
            "provider": "paddleocr",
            "model": model,
            "job_id": job.job_id,
            "task": job.task,
            "page_ranges": page_ranges,
            "options": options_payload,
            "submitted_at": datetime.now(UTC).isoformat(),
        }
        self.store.write_json(raw_dir / "submission.json", submission)
        result = await client.wait_document(job.job_id)
        payload = client.to_dict(result)
        self.store.write_json(raw_dir / "provider_response.json", payload)
        resource_paths = await client.save_document_resources(result, resources_dir)
        latency = time.perf_counter() - start
        stats = self._measure_paddle_payload(
            payload,
            variant=slug,
            model=model,
            page_ranges=page_ranges,
            options=options_payload,
            latency=latency,
            started_at=started_at,
            resource_paths=resource_paths,
            variant_dir=variant_dir,
        )
        stats["latency_mode"] = "submit_wait_and_resource_download"
        self.store.write_json(variant_dir / "stats.json", stats)
        return stats

    async def _reuse_paddle_payload(
        self,
        payload_path: Path,
        variant_dir: Path,
        *,
        slug: str,
        model: str,
        page_ranges: str | None,
    ) -> dict[str, Any]:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        submission_path = variant_dir / "raw" / "submission.json"
        submission = (
            json.loads(submission_path.read_text(encoding="utf-8"))
            if submission_path.exists()
            else {}
        )
        options = submission.get("options") or self._paddle_options(model)[1]
        resources_dir = variant_dir / "resources"
        resource_paths = await PaddleOCRAPIClient(self.settings).save_payload_resources(
            payload, resources_dir
        )
        stats = self._measure_paddle_payload(
            payload,
            variant=slug,
            model=model,
            page_ranges=page_ranges,
            options=options,
            latency=self._submission_elapsed(submission, payload_path),
            started_at=self._submission_time(submission),
            resource_paths=resource_paths,
            variant_dir=variant_dir,
        )
        stats["resumed_from_frozen_response"] = True
        stats["latency_mode"] = "cached_or_resumed_response"
        self.store.write_json(variant_dir / "stats.json", stats)
        return stats

    @staticmethod
    def _paddle_options(model: str) -> tuple[Any, dict[str, Any]]:
        try:
            return PaddleOCRAPIClient.document_options(model)
        except Exception as exc:
            raise ParserBenchmarkError(str(exc)) from exc

    def _measure_paddle_payload(
        self,
        payload: dict[str, Any],
        *,
        variant: str,
        model: str,
        page_ranges: str | None,
        options: dict[str, Any],
        latency: float | None,
        started_at: datetime,
        resource_paths: list[str],
        variant_dir: Path,
    ) -> dict[str, Any]:
        pages = payload.get("pages", [])
        blocks: list[dict[str, Any]] = []
        for page in pages:
            pruned = page.get("pruned_result") or {}
            page_blocks = pruned.get("parsing_res_list") or []
            blocks.extend(block for block in page_blocks if isinstance(block, dict))
        type_counts = Counter(str(block.get("block_label", "unknown")) for block in blocks)
        bbox_count = sum(
            isinstance(block.get("block_bbox"), list) and len(block["block_bbox"]) == 4
            for block in blocks
        )
        text_chars = sum(len(str(block.get("block_content") or "")) for block in blocks)
        files, byte_count = self._tree_size(variant_dir)
        return {
            "variant": variant,
            "provider": "paddleocr",
            "model": model,
            "sdk_version": self._package_version("paddleocr"),
            "provider_job_id": payload.get("job_id") or payload.get("jobId"),
            "page_ranges": page_ranges,
            # The SDK can expose a per-JSONL value of 1 after joining multiple pages.
            # Preserve it as provider metadata, never as the source PDF page count.
            "provider_data_info_num_pages": (payload.get("data_info") or {}).get("numPages"),
            "returned_pages": len(pages),
            "blocks": len(blocks),
            "type_counts": dict(sorted(type_counts.items())),
            "text_chars": text_chars,
            "markdown_chars": sum(len(str(page.get("markdown_text") or "")) for page in pages),
            "bbox_blocks": bbox_count,
            "bbox_coverage": round(bbox_count / len(blocks), 4) if blocks else 0.0,
            "downloaded_resources": len(resource_paths),
            "raw_artifact_files": files,
            "raw_artifact_bytes": byte_count,
            "latency_seconds": round(latency, 3) if latency is not None else None,
            "latency_per_returned_page_seconds": (
                round(latency / len(pages), 3) if latency is not None and pages else None
            ),
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "options": options,
        }

    @staticmethod
    def _page_numbers(page_ranges: str | None, *, total: int) -> set[int]:
        if not page_ranges:
            return set(range(1, total + 1))
        selected: set[int] = set()
        for part in page_ranges.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start, end = int(start_text), int(end_text)
                selected.update(range(start, end + 1))
            else:
                selected.add(int(part))
        if not selected or min(selected) < 1 or max(selected) > total:
            raise ParserBenchmarkError(f"Invalid page range {page_ranges!r} for {total} pages")
        return selected

    @staticmethod
    def _tree_size(root: Path) -> tuple[int, int]:
        paths = [path for path in root.rglob("*") if path.is_file()] if root.exists() else []
        return len(paths), sum(path.stat().st_size for path in paths)

    @staticmethod
    def _mineru_markdown_chars(parse_dir: Path) -> int:
        markdown_files = list((parse_dir / "raw" / "mineru").glob("*.md"))
        return sum(len(path.read_text(encoding="utf-8")) for path in markdown_files)

    @staticmethod
    def _manifest_latency(manifest: dict[str, Any]) -> float | None:
        try:
            start = datetime.fromisoformat(manifest["created_at"])
            end = datetime.fromisoformat(manifest["completed_at"])
            return round((end - start).total_seconds(), 3)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _submission_time(submission: dict[str, Any]) -> datetime:
        try:
            return datetime.fromisoformat(submission["submitted_at"])
        except (KeyError, TypeError, ValueError):
            return datetime.now(UTC)

    @classmethod
    def _submission_elapsed(cls, submission: dict[str, Any], payload_path: Path) -> float | None:
        submitted = cls._submission_time(submission)
        if "submitted_at" not in submission:
            return None
        frozen_at = datetime.fromtimestamp(payload_path.stat().st_mtime, tz=UTC)
        return max(0.0, (frozen_at - submitted).total_seconds())

    def _complete_run(
        self, run_dir: Path, manifest: dict[str, Any], results: list[dict[str, Any]]
    ) -> Path:
        report = {
            "run_id": manifest["run_id"],
            "source_sha256": manifest["source_sha256"],
            "page_ranges": manifest.get("page_ranges"),
            "warning": (
                "These are artifact/structure statistics, not accuracy scores. "
                "Accuracy requires the gold annotations defined in docs/PARSER_BENCHMARK.md."
            ),
            "variants": results,
        }
        self.store.write_json(run_dir / "report.json", report)
        (run_dir / "report.md").write_text(self._markdown_report(report), encoding="utf-8")
        manifest.update(
            {
                "status": "complete",
                "completed_at": datetime.now(UTC).isoformat(),
                "variants": [item["variant"] for item in results],
                "report": str(run_dir / "report.json"),
            }
        )
        self.store.write_json(run_dir / "manifest.json", manifest)
        return run_dir

    @staticmethod
    def _package_version(package: str) -> str | None:
        try:
            return version(package)
        except PackageNotFoundError:
            return None

    @staticmethod
    def _markdown_report(report: dict[str, Any]) -> str:
        lines = [
            f"# Parser benchmark {report['run_id']}",
            "",
            "> 此表仅包含结构与运行统计，不代表解析准确率。准确率必须使用人工 gold set。",
            "",
            f"- Source SHA-256: `{report['source_sha256']}`",
            f"- Pages: `{report.get('page_ranges') or 'all'}`",
            "",
            "| Variant | Pages | Blocks | Text chars | Markdown chars | BBox | Latency (s) | Raw bytes |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for item in report["variants"]:
            latency = item.get("latency_seconds")
            lines.append(
                "| {variant} | {pages} | {blocks} | {text} | {markdown} | {bbox:.1%} | "
                "{latency} | {bytes} |".format(
                    variant=item["variant"],
                    pages=item.get("returned_pages", 0),
                    blocks=item.get("blocks", 0),
                    text=item.get("text_chars", 0),
                    markdown=item.get("markdown_chars", 0),
                    bbox=item.get("bbox_coverage", 0.0),
                    latency=latency if latency is not None else "n/a",
                    bytes=item.get("raw_artifact_bytes", 0),
                )
            )
        lines.extend(
            [
                "",
                "## 下一步",
                "",
                (
                    "对分层抽样页面建立人工元素、阅读顺序、公式、表格与图注 gold 标注，"
                    "再计算准确率和下游 RAG 指标。"
                ),
                "",
            ]
        )
        return "\n".join(lines)
