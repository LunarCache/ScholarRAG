from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from scholarrag.config import Settings
from scholarrag.domain.models import CanonicalDocument, Chunk


class Catalog:
    def __init__(self, settings: Settings) -> None:
        self.path = settings.sqlite_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    title TEXT,
                    source_filename TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS parses (
                    parse_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(document_id),
                    provider TEXT NOT NULL,
                    provider_model TEXT NOT NULL,
                    artifact_dir TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    parse_id TEXT NOT NULL REFERENCES parses(parse_id),
                    document_id TEXT NOT NULL REFERENCES documents(document_id),
                    text TEXT NOT NULL,
                    contextual_text TEXT NOT NULL,
                    page_numbers TEXT NOT NULL,
                    block_ids TEXT NOT NULL,
                    section_path TEXT NOT NULL,
                    sources TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    chunk_level TEXT NOT NULL DEFAULT 'atomic',
                    parent_chunk_id TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    document_id UNINDEXED,
                    text,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
                CREATE INDEX IF NOT EXISTS idx_chunks_parse ON chunks(parse_id);
                CREATE TABLE IF NOT EXISTS document_metadata (
                    document_id TEXT PRIMARY KEY REFERENCES documents(document_id),
                    parse_id TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_edges (
                    document_id TEXT NOT NULL REFERENCES documents(document_id),
                    relation TEXT NOT NULL,
                    target TEXT NOT NULL,
                    parse_id TEXT NOT NULL,
                    PRIMARY KEY(document_id, relation, target, parse_id)
                );
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(chunks)").fetchall()
            }
            if "chunk_level" not in columns:
                connection.execute(
                    "ALTER TABLE chunks ADD COLUMN chunk_level TEXT NOT NULL DEFAULT 'atomic'"
                )
            if "parent_chunk_id" not in columns:
                connection.execute("ALTER TABLE chunks ADD COLUMN parent_chunk_id TEXT")

    def register(
        self,
        document: CanonicalDocument,
        chunks: list[Chunk],
        *,
        source_path: Path,
        artifact_dir: Path,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO documents(document_id,title,source_filename,source_sha256,source_path,created_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET
                    title=excluded.title,
                    source_filename=excluded.source_filename,
                    source_path=excluded.source_path
                """,
                (
                    document.document_id,
                    document.title,
                    document.source_filename,
                    document.source_sha256,
                    str(source_path),
                    document.created_at.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE parses SET active=0 WHERE document_id=?", (document.document_id,)
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO parses(
                    parse_id,document_id,provider,provider_model,artifact_dir,active,created_at
                ) VALUES(?,?,?,?,?,1,?)
                """,
                (
                    document.parse_id,
                    document.document_id,
                    document.provider,
                    document.provider_model,
                    str(artifact_dir),
                    document.created_at.isoformat(),
                ),
            )
            stale_ids = [
                str(row["chunk_id"])
                for row in connection.execute(
                    "SELECT chunk_id FROM chunks WHERE parse_id=?", (document.parse_id,)
                ).fetchall()
            ]
            for chunk_id in stale_ids:
                connection.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk_id,))
            connection.execute("DELETE FROM chunks WHERE parse_id=?", (document.parse_id,))
            for chunk in chunks:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO chunks(
                        chunk_id,parse_id,document_id,text,contextual_text,page_numbers,
                        block_ids,section_path,sources,metadata,chunk_level,parent_chunk_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        chunk.id,
                        chunk.parse_id,
                        chunk.document_id,
                        chunk.text,
                        chunk.contextual_text,
                        json.dumps(chunk.page_numbers),
                        json.dumps(chunk.block_ids),
                        json.dumps(chunk.section_path, ensure_ascii=False),
                        json.dumps(
                            [source.model_dump(mode="json") for source in chunk.sources],
                            ensure_ascii=False,
                        ),
                        json.dumps(chunk.metadata, ensure_ascii=False),
                        chunk.level,
                        chunk.parent_chunk_id,
                    ),
                )
                connection.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk.id,))
                if chunk.level != "parent":
                    connection.execute(
                        "INSERT INTO chunks_fts(chunk_id,document_id,text) VALUES(?,?,?)",
                        (chunk.id, chunk.document_id, chunk.contextual_text),
                    )

    def list_documents(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, p.parse_id, p.provider, p.provider_model, p.artifact_dir,
                       (SELECT COUNT(*) FROM chunks c WHERE c.parse_id=p.parse_id) AS chunk_count
                FROM documents d JOIN parses p ON p.document_id=d.document_id AND p.active=1
                ORDER BY d.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def register_metadata(self, document_id: str, parse_id: str, metadata: dict[str, Any]) -> None:
        from datetime import UTC, datetime

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO document_metadata(document_id,parse_id,metadata,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET
                parse_id=excluded.parse_id,metadata=excluded.metadata,updated_at=excluded.updated_at
                """,
                (
                    document_id,
                    parse_id,
                    json.dumps(metadata, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute("DELETE FROM knowledge_edges WHERE document_id=?", (document_id,))
            for relation, field in (
                ("uses_method", "methods"),
                ("evaluated_on", "datasets"),
                ("reports_metric", "metrics"),
            ):
                for target in metadata.get(field, []):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO knowledge_edges(
                            document_id,relation,target,parse_id
                        ) VALUES(?,?,?,?)
                        """,
                        (document_id, relation, str(target), parse_id),
                    )

    def get_metadata(self, document_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT metadata FROM document_metadata WHERE document_id=?", (document_id,)
            ).fetchone()
        return json.loads(row["metadata"]) if row else None

    def knowledge_graph(self, document_ids: list[str] | None = None) -> dict[str, Any]:
        params: list[Any] = []
        where = ""
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            where = f"WHERE e.document_id IN ({placeholders})"
            params.extend(document_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT e.document_id,d.title,e.relation,e.target,e.parse_id
                FROM knowledge_edges e JOIN documents d ON d.document_id=e.document_id
                {where} ORDER BY e.document_id,e.relation,e.target
                """,
                params,
            ).fetchall()
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []
        for row in rows:
            paper_id = str(row["document_id"])
            target_id = f"{row['relation']}:{row['target']}"
            nodes[paper_id] = {
                "id": paper_id,
                "kind": "paper",
                "label": row["title"] or paper_id,
            }
            nodes[target_id] = {
                "id": target_id,
                "kind": str(row["relation"]),
                "label": str(row["target"]),
            }
            edges.append(
                {
                    "source": paper_id,
                    "target": target_id,
                    "relation": str(row["relation"]),
                }
            )
        return {"nodes": list(nodes.values()), "edges": edges}

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT d.*, p.parse_id, p.provider, p.provider_model, p.artifact_dir
                FROM documents d JOIN parses p ON p.document_id=d.document_id AND p.active=1
                WHERE d.document_id=?
                """,
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def active_parse_ids(self, document_ids: list[str] | None = None) -> list[str]:
        params: list[Any] = []
        where = "active=1"
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            where += f" AND document_id IN ({placeholders})"
            params.extend(document_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT parse_id FROM parses WHERE {where} ORDER BY parse_id", params
            ).fetchall()
        return [str(row["parse_id"]) for row in rows]

    def list_parses(self, document_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT parse_id,document_id,provider,provider_model,artifact_dir,active,created_at
                FROM parses WHERE document_id=? ORDER BY created_at DESC
                """,
                (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def inactive_parse_ids(self, document_id: str | None = None) -> list[str]:
        with self._connect() as connection:
            if document_id:
                rows = connection.execute(
                    "SELECT parse_id FROM parses WHERE active=0 AND document_id=?",
                    (document_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT parse_id FROM parses WHERE active=0").fetchall()
        return [str(row["parse_id"]) for row in rows]

    def get_parse(self, parse_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT parse_id,document_id,provider,provider_model,artifact_dir,active,created_at
                FROM parses WHERE parse_id=?
                """,
                (parse_id,),
            ).fetchone()
        return dict(row) if row else None

    def activate_parse(self, document_id: str, parse_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document_id FROM parses WHERE parse_id=?", (parse_id,)
            ).fetchone()
            if row is None or row["document_id"] != document_id:
                raise ValueError(f"Unknown parse {parse_id!r} for document {document_id!r}")
            connection.execute("UPDATE parses SET active=0 WHERE document_id=?", (document_id,))
            connection.execute("UPDATE parses SET active=1 WHERE parse_id=?", (parse_id,))

    def chunks_by_ids(self, chunk_ids: Iterable[str]) -> dict[str, Chunk]:
        ids = list(chunk_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.* FROM chunks c
                JOIN parses p ON p.parse_id=c.parse_id AND p.active=1
                WHERE c.chunk_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        return {row["chunk_id"]: self._row_to_chunk(row) for row in rows}

    def lexical_search(
        self, query: str, *, document_ids: list[str] | None = None, limit: int = 30
    ) -> list[tuple[str, float]]:
        match_query = self._fts_query(query)
        if not match_query:
            return []
        params: list[Any] = [match_query]
        where = "chunks_fts MATCH ? AND p.active=1"
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            where += f" AND c.document_id IN ({placeholders})"
            params.extend(document_ids)
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT chunks_fts.chunk_id, -bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id
                JOIN parses p ON p.parse_id=c.parse_id
                WHERE {where}
                ORDER BY bm25(chunks_fts) LIMIT ?
                """,
                params,
            ).fetchall()
        return [(row["chunk_id"], float(row["score"])) for row in rows]

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]", query.lower())
        return " OR ".join(f'"{term}"' for term in terms[:32])

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> Chunk:
        return Chunk.model_validate(
            {
                "id": row["chunk_id"],
                "parse_id": row["parse_id"],
                "document_id": row["document_id"],
                "text": row["text"],
                "contextual_text": row["contextual_text"],
                "page_numbers": json.loads(row["page_numbers"]),
                "block_ids": json.loads(row["block_ids"]),
                "section_path": json.loads(row["section_path"]),
                "sources": json.loads(row["sources"]),
                "metadata": json.loads(row["metadata"]),
                "level": row["chunk_level"],
                "parent_chunk_id": row["parent_chunk_id"],
                "token_estimate": max(1, len(row["contextual_text"]) // 4),
            }
        )
