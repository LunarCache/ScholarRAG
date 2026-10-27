from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scholarrag.authorization import check_active_authorization
from scholarrag.config import Settings
from scholarrag.domain.models import CanonicalDocument, Chunk
from scholarrag.ingestion.checkpoints import IngestionJournal, IngestionUncertain
from scholarrag.ingestion.chunker import SemanticChunker
from scholarrag.ingestion.mineru_normalizer import MinerUNormalizer
from scholarrag.ingestion.paddleocr_normalizer import PaddleOCRNormalizer
from scholarrag.ingestion.quality import ParseQualityGate, ParseQualityReport
from scholarrag.ingestion.routing import ParseRouteDecision, ParseRouter
from scholarrag.ingestion.visual_backfill import VisualEvidenceBackfill
from scholarrag.knowledge.service import MetadataExtractor
from scholarrag.providers.mineru import MinerUClient
from scholarrag.providers.paddleocr import PaddleOCRAPIClient
from scholarrag.rag.runs import LeaseLost
from scholarrag.retrieval.index import VectorIndex, VisualIndex
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog


class ParseQualityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParseCandidate:
    document: CanonicalDocument
    chunks: list[Chunk]
    parse_dir: Path
    source_path: Path
    quality: ParseQualityReport


class IngestionService:
    def __init__(
        self,
        settings: Settings,
        *,
        document_id: str | None = None,
        journal: IngestionJournal | None = None,
    ) -> None:
        if document_id is not None and not re.fullmatch(r"doc_[0-9a-f]{32}", document_id):
            raise ValueError("Invalid allocated document ID")
        self.journal = journal
        self.document_id = document_id
        self.settings = settings
        self.store = ArtifactStore(settings)
        self.quality_gate = ParseQualityGate(
            accept_score=settings.parse_quality_accept_score,
            review_score=settings.parse_quality_review_score,
        )
        self.router = ParseRouter(min_score_improvement=settings.parse_route_min_score_improvement)

    def _candidate_record(self, candidate: ParseCandidate) -> dict:
        paths = ["canonical/document.json", "chunks/chunks.json", "quality/report.json"]
        return {
            "state": "ready",
            "parse_dir": str(candidate.parse_dir.resolve()),
            "source_path": str(candidate.source_path.resolve()),
            "hashes": {name: self.store.sha256(candidate.parse_dir / name) for name in paths},
        }

    def _restore_candidate(self, record: dict) -> ParseCandidate:
        check_active_authorization()
        parse_dir = Path(record["parse_dir"])
        if not parse_dir.resolve().is_relative_to(self.settings.artifacts_dir.resolve()):
            raise ValueError("Candidate is outside the artifact store")
        for name, expected in record["hashes"].items():
            if self.store.sha256(parse_dir / name) != expected:
                raise ValueError("Candidate checkpoint artifact changed")
        document = CanonicalDocument.model_validate_json(
            (parse_dir / "canonical/document.json").read_text()
        )
        source = Path(record["source_path"])
        if (
            document.document_id != self.document_id
            or self.store.sha256(source) != document.source_sha256
        ):
            raise ValueError("Candidate checkpoint identity changed")
        chunks = [
            Chunk.model_validate(row)
            for row in json.loads((parse_dir / "chunks/chunks.json").read_text())
        ]
        quality = ParseQualityReport.model_validate_json(
            (parse_dir / "quality/report.json").read_text()
        )
        return ParseCandidate(document, chunks, parse_dir, source, quality)

    def _configuration_checkpoint(self) -> None:
        if self.journal is None:
            return
        keys = [
            "mineru_base_url",
            "mineru_model",
            "mineru_language",
            "mineru_is_ocr",
            "paddle_model",
            "parse_quality_mode",
            "parse_quality_accept_score",
            "parse_quality_review_score",
            "parse_fallback_mode",
            "parse_route_min_score_improvement",
            "ingest_visual_backfill",
            "siliconflow_base_url",
            "embedding_model",
            "multimodal_embedding_model",
            "embedding_dimensions",
            "qdrant_url",
        ]
        configuration = {key: getattr(self.settings, key) for key in keys}
        old = self.journal.load("configuration")
        if old is not None and old != configuration:
            raise ValueError("Ingestion configuration changed; submit a new task")
        self.journal.save("configuration", configuration)

    async def ingest(
        self,
        source: Path,
        *,
        existing_batch_id: str | None = None,
    ) -> tuple[CanonicalDocument, list[Chunk], Path]:
        """Parse with MinerU, optionally evaluate Paddle, then activate exactly one candidate."""
        check_active_authorization()
        self._configuration_checkpoint()
        selected_record = self.journal.load("selected") if self.journal else None
        if selected_record is not None:
            selected = await asyncio.to_thread(
                self._restore_candidate, selected_record["candidate"]
            )
            await self._activate_candidate(
                selected,
                decision=ParseRouteDecision.model_validate(selected_record["decision"]),
                route_path=Path(selected_record["route_path"]),
            )
            return selected.document, selected.chunks, selected.parse_dir
        primary = await self._parse_mineru_candidate(source, existing_batch_id=existing_batch_id)
        fallback: ParseCandidate | None = None
        fallback_error: str | None = None
        if self.router.should_run_fallback(self.settings.parse_fallback_mode, primary.quality):
            if self.settings.paddleocr_access_token is None:
                fallback_error = "PADDLEOCR_ACCESS_TOKEN is not configured"
            else:
                try:
                    fallback = await self._parse_paddle_candidate(source)
                except (IngestionUncertain, LeaseLost):
                    raise
                except Exception as exc:  # noqa: BLE001 - route artifact must preserve failures
                    fallback_error = f"{type(exc).__name__}: {exc}"

        decision = self.router.route(
            primary.quality,
            fallback.quality if fallback else None,
            primary_document=primary.document,
            fallback_document=fallback.document if fallback else None,
        )
        candidates = [primary, *([fallback] if fallback else [])]
        selected = self._selected_candidate(decision, candidates)
        if selected is None:
            route_path = self._persist_route(
                decision,
                candidates,
                actual_selected_parse_id=None,
                fallback_error=fallback_error,
            )
            self._mark_blocked_candidates(candidates, decision, route_path)
            raise ParseQualityError(
                f"No parse was activated: {decision.recommended_action}; {decision.reason}"
            )
        route_path = self._persist_route(
            decision,
            candidates,
            actual_selected_parse_id=selected.document.parse_id,
            fallback_error=fallback_error,
        )
        if self.journal:
            self.journal.save(
                "selected",
                {
                    "candidate": self._candidate_record(selected),
                    "decision": decision.model_dump(mode="json"),
                    "route_path": str(route_path),
                },
            )
        await self._activate_candidate(selected, decision=decision, route_path=route_path)
        self._mark_unselected_candidates(candidates, selected.document.parse_id, route_path)
        return selected.document, selected.chunks, selected.parse_dir

    async def ingest_mineru(
        self,
        source: Path,
        *,
        existing_batch_id: str | None = None,
    ) -> tuple[CanonicalDocument, list[Chunk], Path]:
        """Run MinerU only, retaining the same quality enforcement used by routed ingestion."""
        candidate = await self._parse_mineru_candidate(source, existing_batch_id=existing_batch_id)
        decision = self.router.route(candidate.quality)
        selected = self._selected_candidate(decision, [candidate])
        route_path = self._persist_route(
            decision,
            [candidate],
            actual_selected_parse_id=(candidate.document.parse_id if selected else None),
        )
        if selected is None:
            self._mark_blocked_candidates([candidate], decision, route_path)
            raise ParseQualityError(
                f"Parse {candidate.document.parse_id} was not activated: "
                f"quality {candidate.quality.decision} ({candidate.quality.score:.4f})"
            )
        await self._activate_candidate(candidate, decision=decision, route_path=route_path)
        return candidate.document, candidate.chunks, candidate.parse_dir

    async def _parse_mineru_candidate(
        self,
        source: Path,
        *,
        existing_batch_id: str | None = None,
    ) -> ParseCandidate:
        saved = self.journal.load("mineru") if self.journal else None
        if saved and saved["state"] == "ready":
            return await asyncio.to_thread(self._restore_candidate, saved)
        if saved and saved["state"] == "submitting":
            raise IngestionUncertain("MinerU submission outcome unknown")
        source = source.resolve()
        source_hash = self.store.sha256(source)
        document_id = self.document_id or f"doc_{source_hash[:24]}"
        parse_id = self._parse_id("mineru", self.settings.mineru_model)
        parse_dir = self.store.parse_dir(document_id, parse_id)
        preserved_source, _ = self.store.preserve_source(source, document_id)
        manifest_path = parse_dir / "manifest.json"
        manifest = self._base_manifest(
            document_id=document_id,
            parse_id=parse_id,
            provider="mineru",
            provider_model=self.settings.mineru_model,
            source_hash=source_hash,
            source=source,
            preserved_source=preserved_source,
        )
        self.store.write_json(manifest_path, manifest)

        client = MinerUClient(self.settings)
        try:
            batch_id = saved.get("batch_id") if saved else existing_batch_id
            if batch_id is None:
                if self.journal:
                    self.journal.save("mineru", {"state": "submitting"})

                def allocated(job):
                    self.journal.save("mineru", {"state": "allocated", "batch_id": job.batch_id})

                options = {"on_allocated": allocated} if self.journal else {}
                job = await client.submit_local_file(source, data_id=document_id, **options)
                batch_id = job.batch_id
                manifest["submit_trace_id"] = job.trace_id
                if self.journal:
                    self.journal.save("mineru", {"state": "submitted", "batch_id": batch_id})
                manifest["batch_id"] = batch_id
                self.store.write_json(manifest_path, manifest)
                response = await client.wait_for_result(batch_id)
            else:
                manifest["batch_id"] = batch_id
                self.store.write_json(manifest_path, manifest)
                response = await client.get_batch(batch_id)
                rows = response.get("data", {}).get("extract_result", [])
                if (
                    saved
                    and saved["state"] == "allocated"
                    and (not rows or rows[0].get("state") == "waiting-file")
                ):
                    raise IngestionUncertain("MinerU upload outcome unknown")
                if not rows or rows[0].get("state") != "done":
                    response = await client.wait_for_result(batch_id)
            raw_dir = parse_dir / "raw" / "mineru"
            self.store.write_json(raw_dir / "provider_response.json", response)
            await client.download_and_extract(response, raw_dir)
        except Exception as exc:
            self._mark_failed(manifest_path, manifest, exc)
            checkpoint = self.journal.load("mineru") if self.journal else None
            if checkpoint and checkpoint["state"] in {"submitting", "allocated"}:
                raise IngestionUncertain("MinerU submission or upload outcome unknown") from exc
            raise
        finally:
            await client.close()

        try:
            document = await asyncio.to_thread(
                MinerUNormalizer().normalize,
                raw_dir,
                document_id=document_id,
                parse_id=parse_id,
                source_sha256=source_hash,
                source_filename=source.name,
                provider_model=self.settings.mineru_model,
            )
            manifest["query_trace_id"] = response.get("trace_id")
            candidate = await asyncio.to_thread(
                self._prepare_candidate, document, parse_dir, preserved_source, manifest=manifest
            )
            if self.journal:
                self.journal.save(
                    "mineru",
                    {**(self.journal.load("mineru") or {}), **self._candidate_record(candidate)},
                )
            return candidate
        except Exception as exc:
            self._mark_failed(manifest_path, manifest, exc)
            raise

    async def _parse_paddle_candidate(self, source: Path) -> ParseCandidate:
        check_active_authorization()
        saved = self.journal.load("paddleocr") if self.journal else None
        if saved and saved["state"] == "ready":
            return await asyncio.to_thread(self._restore_candidate, saved)
        if saved and saved["state"] == "submitting":
            raise IngestionUncertain("PaddleOCR submission outcome unknown")
        source = source.resolve()
        source_hash = self.store.sha256(source)
        document_id = self.document_id or f"doc_{source_hash[:24]}"
        parse_id = self._parse_id("paddleocr", self.settings.paddle_model)
        parse_dir = self.store.parse_dir(document_id, parse_id)
        preserved_source, _ = self.store.preserve_source(source, document_id)
        manifest_path = parse_dir / "manifest.json"
        manifest = self._base_manifest(
            document_id=document_id,
            parse_id=parse_id,
            provider="paddleocr",
            provider_model=self.settings.paddle_model,
            source_hash=source_hash,
            source=source,
            preserved_source=preserved_source,
        )
        raw_dir = parse_dir / "raw" / "paddleocr"
        resources_dir = raw_dir / "resources"
        self.store.write_json(manifest_path, manifest)
        client = PaddleOCRAPIClient(self.settings)
        try:
            if saved:
                job_id = saved["job_id"]
                options_payload = saved["options"]
            else:
                options, options_payload = client.document_options(self.settings.paddle_model)
                if self.journal:
                    self.journal.save("paddleocr", {"state": "submitting"})
                job = await client.submit_document(
                    source, model=self.settings.paddle_model, options=options
                )
                submission = client.to_dict(job)
                job_id = (
                    submission.get("job_id")
                    or submission.get("jobId")
                    or getattr(job, "job_id", None)
                )
                if not job_id:
                    raise RuntimeError("PaddleOCR submission did not return a job ID")
                if self.journal:
                    self.journal.save(
                        "paddleocr",
                        {"state": "submitted", "job_id": str(job_id), "options": options_payload},
                    )
                self.store.write_json(raw_dir / "submission.json", submission)
            manifest["job_id"] = str(job_id)
            manifest["options"] = options_payload
            self.store.write_json(manifest_path, manifest)
            result = await client.wait_document(str(job_id))
            payload = client.to_dict(result)
            payload_path = self.store.write_json(raw_dir / "provider_response.json", payload)
            resource_paths = await client.save_document_resources(result, resources_dir)
            manifest["resource_count"] = len(resource_paths)
        except Exception as exc:
            self._mark_failed(manifest_path, manifest, exc)
            checkpoint = self.journal.load("paddleocr") if self.journal else None
            if checkpoint and checkpoint["state"] == "submitting":
                raise IngestionUncertain("PaddleOCR submission outcome unknown") from exc
            raise

        document = await asyncio.to_thread(
            PaddleOCRNormalizer().normalize,
            payload_path,
            resource_dir=resources_dir,
            output_dir=parse_dir / "canonical",
            source_pdf=preserved_source,
            document_id=document_id,
            parse_id=parse_id,
            source_sha256=source_hash,
            source_filename=source.name,
            provider_model=self.settings.paddle_model,
        )
        manifest["provider_job_id"] = document.metadata.get("provider_job_id")
        candidate = await asyncio.to_thread(
            self._prepare_candidate, document, parse_dir, preserved_source, manifest=manifest
        )
        if self.journal:
            self.journal.save(
                "paddleocr",
                {**(self.journal.load("paddleocr") or {}), **self._candidate_record(candidate)},
            )
        return candidate

    def _prepare_candidate(
        self,
        document: CanonicalDocument,
        parse_dir: Path,
        preserved_source: Path,
        *,
        manifest: dict[str, Any],
    ) -> ParseCandidate:
        chunks = SemanticChunker().chunk(document)
        if self.settings.ingest_visual_backfill:
            VisualEvidenceBackfill().fill(chunks, document, preserved_source, parse_dir / "visual")
        canonical_path = parse_dir / "canonical" / "document.json"
        chunks_path = parse_dir / "chunks" / "chunks.json"
        self.store.write_json(canonical_path, document.model_dump(mode="json"))
        self.store.write_json(chunks_path, [chunk.model_dump(mode="json") for chunk in chunks])
        quality = self.quality_gate.evaluate(
            document, expected_page_count=self._pdf_page_count(preserved_source)
        )
        quality_path = parse_dir / "quality" / "report.json"
        self.store.write_json(quality_path, quality.model_dump(mode="json"))
        manifest.update(
            {
                "status": "candidate_ready",
                "parsed_at": datetime.now(UTC).isoformat(),
                "page_count": len(document.pages),
                "block_count": len(document.blocks),
                "chunk_count": len(chunks),
                "canonical_artifact": str(canonical_path),
                "chunks_artifact": str(chunks_path),
                "quality": {
                    "score": quality.score,
                    "decision": quality.decision,
                    "critical_failures": quality.critical_failures,
                    "report_artifact": str(quality_path),
                },
            }
        )
        self.store.write_json(parse_dir / "manifest.json", manifest)
        return ParseCandidate(document, chunks, parse_dir, preserved_source, quality)

    def _selected_candidate(
        self,
        decision: ParseRouteDecision,
        candidates: list[ParseCandidate],
    ) -> ParseCandidate | None:
        selected_id = decision.selected_parse_id
        if selected_id is None and self.settings.parse_quality_mode == "audit":
            selected_id = decision.primary_parse_id
        selected = next(
            (candidate for candidate in candidates if candidate.document.parse_id == selected_id),
            None,
        )
        if (
            selected is not None
            and self.settings.parse_quality_mode == "enforce"
            and selected.quality.decision != "accept"
        ):
            return None
        return selected

    async def _activate_candidate(
        self,
        candidate: ParseCandidate,
        *,
        decision: ParseRouteDecision,
        route_path: Path,
    ) -> None:
        check_active_authorization()
        if self.settings.parse_quality_mode == "enforce" and decision.recommended_action in {
            "manual_review",
            "run_fallback",
        }:
            raise ParseQualityError(
                f"Parse route {decision.route_id} requires {decision.recommended_action}"
            )

        async def index_stage(key, operation):
            saved = self.journal.load(key) if self.journal else None
            if saved:
                if saved["parse_id"] != candidate.document.parse_id:
                    raise ValueError("Index checkpoint parse changed")
                if saved["state"] != "done":
                    raise IngestionUncertain("Embedding/index outcome unknown")
                return saved["count"]
            if self.journal:
                self.journal.save(
                    key, {"state": "running", "parse_id": candidate.document.parse_id}
                )
            result = await operation()
            if self.journal:
                self.journal.save(
                    key, {"state": "done", "parse_id": candidate.document.parse_id, "count": result}
                )
            return result

        indexed_count = await index_stage(
            "text_index", lambda: VectorIndex(self.settings).index(candidate.chunks)
        )
        visual_indexed_count = await index_stage(
            "visual_index",
            lambda: VisualIndex(self.settings).index(candidate.document, candidate.chunks),
        )
        check_active_authorization()
        if self.journal is None:
            catalog = Catalog(self.settings)
            catalog.register(
                candidate.document,
                candidate.chunks,
                source_path=candidate.source_path,
                artifact_dir=candidate.parse_dir,
            )
            metadata = MetadataExtractor.extract(candidate.document)
            catalog.register_metadata(
                candidate.document.document_id,
                candidate.document.parse_id,
                metadata.model_dump(mode="json"),
            )
        manifest_path = candidate.parse_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "status": "ready",
                "completed_at": datetime.now(UTC).isoformat(),
                "indexed_count": indexed_count,
                "visual_indexed_count": visual_indexed_count,
                "routing": {
                    "mode": self.settings.parse_quality_mode,
                    "fallback_mode": self.settings.parse_fallback_mode,
                    "route_id": decision.route_id,
                    "route_artifact": str(route_path),
                    "recommended_action": decision.recommended_action,
                    "selected_parse_id": candidate.document.parse_id,
                    "activation_reason": (
                        decision.reason
                        if decision.selected_parse_id == candidate.document.parse_id
                        else "audit_mode_retained_primary"
                    ),
                },
            }
        )
        self.store.write_json(manifest_path, manifest)

    def _persist_route(
        self,
        decision: ParseRouteDecision,
        candidates: list[ParseCandidate],
        *,
        actual_selected_parse_id: str | None,
        fallback_error: str | None = None,
    ) -> Path:
        document_id = candidates[0].document.document_id
        route_path = self.store.document_dir(document_id) / "routing" / f"{decision.route_id}.json"
        payload = decision.model_dump(mode="json")
        payload["execution"] = {
            "quality_mode": self.settings.parse_quality_mode,
            "fallback_mode": self.settings.parse_fallback_mode,
            "actual_selected_parse_id": actual_selected_parse_id,
            "deviated_from_recommendation": (
                actual_selected_parse_id != decision.selected_parse_id
            ),
            "fallback_error": fallback_error,
        }
        payload["candidate_artifacts"] = {
            candidate.document.parse_id: str(candidate.parse_dir) for candidate in candidates
        }
        return self.store.write_json(route_path, payload)

    def _mark_unselected_candidates(
        self,
        candidates: list[ParseCandidate],
        selected_parse_id: str,
        route_path: Path,
    ) -> None:
        for candidate in candidates:
            if candidate.document.parse_id == selected_parse_id:
                continue
            manifest_path = candidate.parse_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "status": "candidate_not_selected",
                    "routing": {
                        "route_artifact": str(route_path),
                        "selected_parse_id": selected_parse_id,
                        "activation_reason": "candidate_not_selected_by_router",
                    },
                }
            )
            self.store.write_json(manifest_path, manifest)

    def _mark_blocked_candidates(
        self,
        candidates: list[ParseCandidate],
        decision: ParseRouteDecision,
        route_path: Path,
    ) -> None:
        status = (
            "fallback_required"
            if decision.recommended_action == "run_fallback"
            else "quality_review_required"
        )
        for candidate in candidates:
            manifest_path = candidate.parse_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "status": status,
                    "routing": {
                        "route_id": decision.route_id,
                        "route_artifact": str(route_path),
                        "recommended_action": decision.recommended_action,
                        "selected_parse_id": None,
                        "activation_reason": decision.reason,
                    },
                }
            )
            self.store.write_json(manifest_path, manifest)

    @staticmethod
    def _mark_failed(path: Path, manifest: dict[str, Any], exc: Exception) -> None:
        manifest["status"] = "failed"
        manifest["error_type"] = type(exc).__name__
        manifest["error"] = str(exc)
        ArtifactStore.write_json(path, manifest)

    @staticmethod
    def _base_manifest(
        *,
        document_id: str,
        parse_id: str,
        provider: str,
        provider_model: str,
        source_hash: str,
        source: Path,
        preserved_source: Path,
    ) -> dict[str, Any]:
        return {
            "document_id": document_id,
            "parse_id": parse_id,
            "status": "submitted",
            "provider": provider,
            "provider_model": provider_model,
            "source_sha256": source_hash,
            "source_filename": source.name,
            "preserved_source": str(preserved_source),
            "created_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _parse_id(provider: str, model: str) -> str:
        slug = "".join(character.lower() if character.isalnum() else "-" for character in model)
        slug = "-".join(part for part in slug.split("-") if part)
        return (
            f"{provider}-{slug}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid.uuid4().hex[:6]}"
        )

    @staticmethod
    def _pdf_page_count(source: Path) -> int:
        import pymupdf

        with pymupdf.open(source) as pdf:
            return pdf.page_count
