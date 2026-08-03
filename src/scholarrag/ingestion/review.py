from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from scholarrag.config import Settings
from scholarrag.domain.models import CanonicalDocument, Chunk
from scholarrag.knowledge.service import MetadataExtractor
from scholarrag.retrieval.index import VectorIndex, VisualIndex
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog


class ParseReviewError(RuntimeError):
    pass


class ParseVersion(BaseModel):
    document_id: str
    parse_id: str
    provider: str
    provider_model: str
    status: str
    active: bool
    can_activate: bool
    created_at: str | None = None
    page_count: int | None = None
    block_count: int | None = None
    chunk_count: int | None = None
    quality: dict[str, Any] | None = None
    routing: dict[str, Any] | None = None
    artifact_dir: str


class ParseReviewResult(BaseModel):
    review_id: str
    document_id: str
    parse_id: str
    previous_parse_id: str | None
    active_parse_id: str
    indexed: bool
    no_op: bool
    reason: str
    force: bool
    review_artifact: str


class ParseReviewService:
    """List and manually activate parse candidates while preserving an audit record."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = ArtifactStore(settings)
        self.catalog = Catalog(settings)
        self._activation_lock = asyncio.Lock()

    def list_versions(self, document_id: str) -> list[ParseVersion]:
        parses_root = self._document_dir(document_id) / "parses"
        if not parses_root.is_dir():
            return []
        registered = {row["parse_id"]: row for row in self.catalog.list_parses(document_id)}
        versions: list[ParseVersion] = []
        for parse_dir in sorted(parses_root.iterdir(), reverse=True):
            if not parse_dir.is_dir():
                continue
            manifest_path = parse_dir / "manifest.json"
            if not manifest_path.is_file():
                continue
            manifest = self._read_json(manifest_path)
            if manifest.get("document_id") != document_id:
                continue
            parse_id = str(manifest.get("parse_id") or parse_dir.name)
            record = registered.get(parse_id)
            status = str(manifest.get("status") or "unknown")
            quality = self._load_quality(parse_dir, manifest)
            has_artifacts = (parse_dir / "canonical" / "document.json").is_file() and (
                parse_dir / "chunks" / "chunks.json"
            ).is_file()
            versions.append(
                ParseVersion(
                    document_id=document_id,
                    parse_id=parse_id,
                    provider=str(manifest.get("provider") or "unknown"),
                    provider_model=str(manifest.get("provider_model") or "unknown"),
                    status=status,
                    active=bool(record and record["active"]),
                    can_activate=has_artifacts and status not in {"submitted", "parsing", "failed"},
                    created_at=manifest.get("created_at"),
                    page_count=manifest.get("page_count"),
                    block_count=manifest.get("block_count"),
                    chunk_count=manifest.get("chunk_count"),
                    quality=quality,
                    routing=manifest.get("routing"),
                    artifact_dir=str(parse_dir),
                )
            )
        return sorted(
            versions,
            key=lambda version: (version.created_at or "", version.parse_id),
            reverse=True,
        )

    def list_routes(self, document_id: str) -> list[dict[str, Any]]:
        route_root = self._document_dir(document_id) / "routing"
        if not route_root.is_dir():
            return []
        routes = [
            self._read_json(path) for path in sorted(route_root.glob("route_*.json"), reverse=True)
        ]
        return routes

    async def activate(
        self,
        document_id: str,
        parse_id: str,
        *,
        reason: str,
        force: bool = False,
    ) -> ParseReviewResult:
        reason = reason.strip()
        if len(reason) < 3:
            raise ParseReviewError("A review reason of at least 3 characters is required")
        async with self._activation_lock:
            parse_dir = self._parse_dir(document_id, parse_id)
            manifest_path = parse_dir / "manifest.json"
            if not manifest_path.is_file():
                raise ParseReviewError(f"Unknown parse: {parse_id}")
            manifest = self._read_json(manifest_path)
            if manifest.get("document_id") != document_id or manifest.get("parse_id") != parse_id:
                raise ParseReviewError("Parse manifest identity does not match the requested path")
            version = next(
                (item for item in self.list_versions(document_id) if item.parse_id == parse_id),
                None,
            )
            if version is None or not version.can_activate:
                raise ParseReviewError(f"Parse {parse_id} is not activatable")
            quality = self._load_quality(parse_dir, manifest)
            quality_decision = str((quality or {}).get("decision") or "unknown")
            if quality_decision == "fallback" and not force:
                raise ParseReviewError(
                    "This parse failed the quality gate; set force=true after manual inspection"
                )

            current = self.catalog.get_document(document_id)
            previous_parse_id = str(current["parse_id"]) if current else None
            no_op = previous_parse_id == parse_id
            indexed = False
            if not no_op:
                registered = self.catalog.get_parse(parse_id)
                if registered is not None:
                    self.catalog.activate_parse(document_id, parse_id)
                else:
                    document, chunks = self._load_candidate(parse_dir, document_id, parse_id)
                    try:
                        indexed_count = await VectorIndex(self.settings).index(chunks)
                        visual_indexed_count = await VisualIndex(self.settings).index(
                            document, chunks
                        )
                    except Exception as exc:
                        attempt_id = (
                            "activation_"
                            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_"
                            f"{uuid.uuid4().hex[:8]}"
                        )
                        attempt_path = parse_dir / "reviews" / f"{attempt_id}.json"
                        attempt = {
                            "schema_version": "1.0",
                            "attempt_id": attempt_id,
                            "created_at": datetime.now(UTC).isoformat(),
                            "actor": "local_user",
                            "document_id": document_id,
                            "parse_id": parse_id,
                            "previous_parse_id": previous_parse_id,
                            "decision": "activation_failed",
                            "reason": reason,
                            "force": force,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        self.store.write_json(attempt_path, attempt)
                        attempts = manifest.get("activation_attempts")
                        if not isinstance(attempts, list):
                            attempts = []
                        attempts.append(
                            {
                                "attempt_id": attempt_id,
                                "created_at": attempt["created_at"],
                                "status": "failed",
                                "artifact": str(attempt_path),
                            }
                        )
                        manifest["status"] = "activation_failed"
                        manifest["activation_error"] = attempt["error"]
                        manifest["activation_attempts"] = attempts
                        self.store.write_json(manifest_path, manifest)
                        raise
                    self.catalog.register(
                        document,
                        chunks,
                        source_path=Path(manifest["preserved_source"]),
                        artifact_dir=parse_dir,
                    )
                    metadata = MetadataExtractor.extract(document)
                    self.catalog.register_metadata(
                        document.document_id,
                        document.parse_id,
                        metadata.model_dump(mode="json"),
                    )
                    manifest["indexed_count"] = indexed_count
                    manifest["visual_indexed_count"] = visual_indexed_count
                    indexed = True

            review_id = (
                f"review_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
            )
            review_path = parse_dir / "reviews" / f"{review_id}.json"
            review = {
                "schema_version": "1.0",
                "review_id": review_id,
                "created_at": datetime.now(UTC).isoformat(),
                "actor": "local_user",
                "document_id": document_id,
                "parse_id": parse_id,
                "previous_parse_id": previous_parse_id,
                "decision": "activate",
                "reason": reason,
                "force": force,
                "quality_at_review": quality,
                "route_at_review": manifest.get("routing"),
                "indexed_during_review": indexed,
                "no_op_already_active": no_op,
            }
            self.store.write_json(review_path, review)
            manifest.update(
                {
                    "status": "ready",
                    "manual_review": {
                        "review_id": review_id,
                        "review_artifact": str(review_path),
                        "reviewed_at": review["created_at"],
                        "reason": reason,
                        "force": force,
                    },
                }
            )
            manifest.pop("activation_error", None)
            attempts = manifest.get("activation_attempts")
            if not isinstance(attempts, list):
                attempts = []
            attempts.append(
                {
                    "attempt_id": review_id,
                    "created_at": review["created_at"],
                    "status": "success",
                    "artifact": str(review_path),
                }
            )
            manifest["activation_attempts"] = attempts
            self.store.write_json(manifest_path, manifest)
            return ParseReviewResult(
                review_id=review_id,
                document_id=document_id,
                parse_id=parse_id,
                previous_parse_id=previous_parse_id,
                active_parse_id=parse_id,
                indexed=indexed,
                no_op=no_op,
                reason=reason,
                force=force,
                review_artifact=str(review_path),
            )

    def _load_candidate(
        self, parse_dir: Path, document_id: str, parse_id: str
    ) -> tuple[CanonicalDocument, list[Chunk]]:
        canonical_path = parse_dir / "canonical" / "document.json"
        chunks_path = parse_dir / "chunks" / "chunks.json"
        if not canonical_path.is_file() or not chunks_path.is_file():
            raise ParseReviewError("Canonical document or chunks artifact is missing")
        document = CanonicalDocument.model_validate_json(canonical_path.read_text(encoding="utf-8"))
        chunks = [
            Chunk.model_validate(item)
            for item in json.loads(chunks_path.read_text(encoding="utf-8"))
        ]
        if document.document_id != document_id or document.parse_id != parse_id:
            raise ParseReviewError("Canonical document identity does not match the review target")
        if any(chunk.document_id != document_id or chunk.parse_id != parse_id for chunk in chunks):
            raise ParseReviewError("Chunk identity does not match the review target")
        return document, chunks

    def _document_dir(self, document_id: str) -> Path:
        root = self.settings.artifacts_dir.resolve()
        target = (root / document_id).resolve()
        if target.parent != root:
            raise ParseReviewError("Invalid document ID")
        return target

    def _parse_dir(self, document_id: str, parse_id: str) -> Path:
        parses_root = (self._document_dir(document_id) / "parses").resolve()
        target = (parses_root / parse_id).resolve()
        if target.parent != parses_root:
            raise ParseReviewError("Invalid parse ID")
        return target

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ParseReviewError(f"Expected a JSON object: {path}")
        return payload

    def _load_quality(self, parse_dir: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
        quality = manifest.get("quality")
        if isinstance(quality, dict):
            return quality
        report_path = parse_dir / "quality" / "report.json"
        if report_path.is_file():
            return self._read_json(report_path)
        return None
