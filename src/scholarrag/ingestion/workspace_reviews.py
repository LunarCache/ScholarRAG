"""Authorized switching between already registered/indexed parse versions."""

from __future__ import annotations

import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from scholarrag.domain.access import AccessDenied, IdentityConflict
from scholarrag.ingestion.review import ParseReviewError, ParseReviewService
from scholarrag.knowledge.service import MetadataExtractor
from scholarrag.storage.identity import IdentityStore


class WorkspaceParseActivation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=0)
    expected_parse_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=2000)
    force: bool = False


class WorkspaceParseReviews:
    def __init__(self, settings, identities: IdentityStore):
        self.identities = identities
        self.reviews = ParseReviewService(settings)
        self.catalog = self.reviews.catalog
        with identities._transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS workspace_parse_reviews (
                review_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                document_id TEXT NOT NULL, actor_id TEXT NOT NULL, parse_id TEXT NOT NULL,
                previous_parse_id TEXT NOT NULL, revision INTEGER NOT NULL,
                reason TEXT NOT NULL, force INTEGER NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(workspace_id,document_id,revision))""")

    def _document(self, connection, context, document_id):
        if document_id not in IdentityStore.active_document_ids(connection, context):
            raise AccessDenied("Document unavailable")
        return connection.execute(
            """SELECT d.*,p.parse_id FROM documents d JOIN parses p
            ON p.document_id=d.document_id AND p.active=1 WHERE d.document_id=?""",
            (document_id,),
        ).fetchone()

    @staticmethod
    def _revision(connection, context, document_id):
        return connection.execute(
            """SELECT COALESCE(MAX(revision),0) FROM workspace_parse_reviews
            WHERE workspace_id=? AND document_id=?""",
            (context.workspace_id, document_id),
        ).fetchone()[0]

    def snapshot(self, context, document_id):
        with self.identities.authorized_transaction(context, "read") as connection:
            document = self._document(connection, context, document_id)
            registered = {
                row[0]
                for row in connection.execute(
                    "SELECT parse_id FROM parses WHERE document_id=?", (document_id,)
                )
            }
            try:
                versions = self.reviews.list_versions(document_id)
            except (OSError, ValueError, ParseReviewError) as exc:
                raise IdentityConflict("Parse artifacts unavailable") from exc
            result = []
            for version in versions:
                quality = version.quality or {}
                result.append(
                    {
                        "parse_id": version.parse_id,
                        "provider": version.provider,
                        "provider_model": version.provider_model,
                        "status": version.status,
                        "active": version.parse_id == document["parse_id"],
                        "can_activate": version.can_activate and version.parse_id in registered,
                        "requires_indexing": version.parse_id not in registered,
                        "created_at": version.created_at,
                        "page_count": version.page_count,
                        "chunk_count": version.chunk_count,
                        "quality_score": quality.get("score"),
                        "quality_decision": quality.get("decision"),
                    }
                )
            history = [
                dict(row)
                for row in connection.execute(
                    """SELECT review_id,actor_id,parse_id,previous_parse_id,revision,reason,
                    force,created_at FROM workspace_parse_reviews
                    WHERE workspace_id=? AND document_id=? ORDER BY revision DESC LIMIT 50""",
                    (context.workspace_id, document_id),
                )
            ]
            return {
                "active_parse_id": document["parse_id"],
                "revision": self._revision(connection, context, document_id),
                "versions": result,
                "history": history,
            }

    def activate(self, context, document_id, parse_id, request: WorkspaceParseActivation):
        # No provider calls or file writes inside this transaction. Switching and
        # its authoritative review/audit records either all commit or all roll back.
        with self.identities.authorized_transaction(
            context, "manage_documents", write=True
        ) as connection:
            document = self._document(connection, context, document_id)
            revision = self._revision(connection, context, document_id)
            if (
                revision != request.expected_revision
                or document["parse_id"] != request.expected_parse_id
            ):
                raise IdentityConflict("Parse review changed; reload before retrying")
            target = connection.execute(
                "SELECT * FROM parses WHERE document_id=? AND parse_id=?", (document_id, parse_id)
            ).fetchone()
            if target is None:
                raise IdentityConflict("Parse is not registered; index preparation required")
            try:
                parse_dir = self.reviews._parse_dir(document_id, parse_id)
                if Path(target["artifact_dir"]).resolve() != parse_dir:
                    raise ParseReviewError("Unexpected artifact directory")
                manifest = self.reviews._read_json(parse_dir / "manifest.json")
                if (
                    manifest.get("document_id") != document_id
                    or manifest.get("parse_id") != parse_id
                ):
                    raise ParseReviewError("Parse identity mismatch")
                if manifest.get("status") in {"submitted", "parsing", "failed"}:
                    raise ParseReviewError("Parse unavailable")
                candidate, _ = self.reviews._load_candidate(parse_dir, document_id, parse_id)
                if candidate.source_sha256 != document["source_sha256"]:
                    raise ParseReviewError("Source identity mismatch")
                quality = self.reviews._load_quality(parse_dir, manifest) or {}
                if quality.get("decision") == "fallback" and not request.force:
                    raise ParseReviewError("Quality gate requires explicit force confirmation")
            except (OSError, ValueError, ParseReviewError) as exc:
                raise IdentityConflict(
                    "Parse unavailable or quality confirmation required"
                ) from exc
            metadata = MetadataExtractor.extract(candidate).model_dump(mode="json")
            connection.execute("UPDATE parses SET active=0 WHERE document_id=?", (document_id,))
            connection.execute("UPDATE parses SET active=1 WHERE parse_id=?", (parse_id,))
            connection.execute(
                "UPDATE documents SET title=? WHERE document_id=?", (candidate.title, document_id)
            )
            self.catalog.register_metadata(
                document_id, parse_id, metadata, external_connection=connection
            )
            review_id = f"review_{uuid.uuid4().hex}"
            from scholarrag.rag.runs import utc_now

            connection.execute(
                "INSERT INTO workspace_parse_reviews VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    review_id,
                    context.workspace_id,
                    document_id,
                    context.user_id,
                    parse_id,
                    document["parse_id"],
                    revision + 1,
                    request.reason,
                    int(request.force),
                    utc_now().isoformat(),
                ),
            )
            self.identities._audit(
                connection, context.user_id, context.workspace_id, "parse.activated", review_id
            )
            return {"review_id": review_id, "active_parse_id": parse_id, "revision": revision + 1}
