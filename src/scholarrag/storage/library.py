from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from scholarrag.config import Settings


class PersonalPaperState(BaseModel):
    document_id: str
    reading_status: Literal["unread", "reading", "finished"] = "unread"
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    favorite: bool = False
    updated_at: str


class Collection(BaseModel):
    collection_id: str
    name: str
    document_ids: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class PersonalLibraryStore:
    def __init__(self, settings: Settings) -> None:
        self.path = settings.sqlite_path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS personal_paper_state (
                    document_id TEXT PRIMARY KEY,
                    reading_status TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    note TEXT NOT NULL,
                    favorite INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collections (
                    collection_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    document_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def get_state(self, document_id: str) -> PersonalPaperState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM personal_paper_state WHERE document_id=?", (document_id,)
            ).fetchone()
        if row:
            return self._state(row)
        return PersonalPaperState(
            document_id=document_id,
            updated_at=datetime.now(UTC).isoformat(),
        )

    def save_state(self, state: PersonalPaperState) -> PersonalPaperState:
        timestamp = datetime.now(UTC).isoformat()
        value = state.model_copy(update={"updated_at": timestamp})
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO personal_paper_state(
                    document_id,reading_status,tags,note,favorite,updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET
                    reading_status=excluded.reading_status,tags=excluded.tags,
                    note=excluded.note,favorite=excluded.favorite,updated_at=excluded.updated_at
                """,
                (
                    value.document_id,
                    value.reading_status,
                    json.dumps(value.tags, ensure_ascii=False),
                    value.note,
                    int(value.favorite),
                    timestamp,
                ),
            )
        return value

    def list_collections(self) -> list[Collection]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM collections ORDER BY updated_at DESC"
            ).fetchall()
        return [self._collection(row) for row in rows]

    def save_collection(self, name: str, document_ids: list[str]) -> Collection:
        timestamp = datetime.now(UTC).isoformat()
        value = Collection(
            collection_id=f"col_{uuid.uuid4().hex}",
            name=name.strip(),
            document_ids=list(dict.fromkeys(document_ids)),
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO collections(collection_id,name,document_ids,created_at,updated_at)
                VALUES(?,?,?,?,?)
                """,
                (
                    value.collection_id,
                    value.name,
                    json.dumps(value.document_ids),
                    value.created_at,
                    value.updated_at,
                ),
            )
        return value

    @staticmethod
    def _state(row: sqlite3.Row) -> PersonalPaperState:
        payload = dict(row)
        payload["tags"] = json.loads(payload["tags"])
        payload["favorite"] = bool(payload["favorite"])
        return PersonalPaperState.model_validate(payload)

    @staticmethod
    def _collection(row: sqlite3.Row) -> Collection:
        payload: dict[str, Any] = dict(row)
        payload["document_ids"] = json.loads(payload["document_ids"])
        return Collection.model_validate(payload)
