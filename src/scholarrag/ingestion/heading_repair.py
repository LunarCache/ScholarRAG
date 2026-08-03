from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scholarrag.config import Settings
from scholarrag.domain.models import CanonicalDocument
from scholarrag.ingestion.chunker import SemanticChunker
from scholarrag.ingestion.mineru_normalizer import MinerUNormalizer
from scholarrag.ingestion.paddleocr_normalizer import PaddleOCRNormalizer
from scholarrag.ingestion.quality import ParseQualityGate
from scholarrag.ingestion.service import IngestionService
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog


class HeadingRepairError(RuntimeError):
    pass


class HeadingRepairService:
    """Create an append-only parse derived from existing paid provider artifacts."""

    transformation = "heading_hierarchy_repair_v1"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = ArtifactStore(settings)
        self.catalog = Catalog(settings)
        self.quality_gate = ParseQualityGate(
            accept_score=settings.parse_quality_accept_score,
            review_score=settings.parse_quality_review_score,
        )

    def prepare(self, document_id: str, source_parse_id: str | None = None) -> dict[str, Any]:
        source_parse_id = source_parse_id or self._active_parse_id(document_id)
        source_dir = self._parse_dir(document_id, source_parse_id)
        source_manifest = self._read_object(source_dir / "manifest.json")
        source_document = CanonicalDocument.model_validate_json(
            (source_dir / "canonical" / "document.json").read_text(encoding="utf-8")
        )
        if source_document.document_id != document_id:
            raise HeadingRepairError("Source canonical document identity does not match")

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        parse_id = f"{source_parse_id}-heading-{timestamp}-{uuid.uuid4().hex[:6]}"
        parse_dir = self.store.parse_dir(document_id, parse_id)
        provider = str(source_manifest.get("provider") or source_document.provider)
        provider_model = str(
            source_manifest.get("provider_model") or source_document.provider_model
        )
        source_path = Path(
            str(source_manifest.get("preserved_source") or self._source_path(document_id))
        )

        if provider == "mineru":
            raw_dir = source_dir / "raw" / "mineru"
            document = MinerUNormalizer().normalize(
                raw_dir,
                document_id=document_id,
                parse_id=parse_id,
                source_sha256=source_document.source_sha256,
                source_filename=source_document.source_filename,
                provider_model=provider_model,
            )
        elif provider == "paddleocr":
            raw_dir = source_dir / "raw" / "paddleocr"
            document = PaddleOCRNormalizer().normalize(
                raw_dir / "provider_response.json",
                resource_dir=raw_dir / "resources",
                output_dir=parse_dir / "canonical",
                source_pdf=source_path,
                document_id=document_id,
                parse_id=parse_id,
                source_sha256=source_document.source_sha256,
                source_filename=source_document.source_filename,
                provider_model=provider_model,
            )
        else:
            raise HeadingRepairError(f"Unsupported provider for repair: {provider}")

        chunks = SemanticChunker().chunk(document)
        expected_pages = IngestionService._pdf_page_count(source_path)
        quality = self.quality_gate.evaluate(document, expected_page_count=expected_pages)
        canonical_path = self.store.write_json(
            parse_dir / "canonical" / "document.json", document.model_dump(mode="json")
        )
        chunks_path = self.store.write_json(
            parse_dir / "chunks" / "chunks.json",
            [chunk.model_dump(mode="json") for chunk in chunks],
        )
        quality_path = self.store.write_json(
            parse_dir / "quality" / "report.json", quality.model_dump(mode="json")
        )
        manifest = {
            "schema_version": "1.0",
            "document_id": document_id,
            "parse_id": parse_id,
            "provider": provider,
            "provider_model": provider_model,
            "status": "candidate_ready",
            "created_at": datetime.now(UTC).isoformat(),
            "preserved_source": str(source_path),
            "source_sha256": source_document.source_sha256,
            "source_filename": source_document.source_filename,
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
            "derivation": {
                "source_parse_id": source_parse_id,
                "source_manifest": str(source_dir / "manifest.json"),
                "transformation": self.transformation,
                "paid_provider_call": False,
                "raw_artifacts_reused": True,
            },
        }
        manifest_path = self.store.write_json(parse_dir / "manifest.json", manifest)
        return {
            "document_id": document_id,
            "source_parse_id": source_parse_id,
            "parse_id": parse_id,
            "provider": provider,
            "chunk_count": len(chunks),
            "quality_score": quality.score,
            "quality_decision": quality.decision,
            "manifest_path": str(manifest_path),
            "parse_dir": str(parse_dir),
        }

    def _active_parse_id(self, document_id: str) -> str:
        document = self.catalog.get_document(document_id)
        if document is None:
            raise HeadingRepairError(f"Unknown document: {document_id}")
        return str(document["parse_id"])

    def _parse_dir(self, document_id: str, parse_id: str) -> Path:
        root = self.settings.artifacts_dir.resolve()
        target = (root / document_id / "parses" / parse_id).resolve()
        if target.parent.parent.parent != root:
            raise HeadingRepairError("Invalid document or parse ID")
        if not target.is_dir():
            raise HeadingRepairError(f"Unknown parse: {parse_id}")
        return target

    def _source_path(self, document_id: str) -> Path:
        sources = list((self.settings.artifacts_dir / document_id / "source").glob("*.pdf"))
        if len(sources) != 1:
            raise HeadingRepairError(f"Expected one preserved source PDF, found {len(sources)}")
        return sources[0]

    @staticmethod
    def _read_object(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise HeadingRepairError(f"Missing artifact: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise HeadingRepairError(f"Expected JSON object: {path}")
        return payload
