from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from scholarrag.config import Settings


class ProviderCache:
    def __init__(self, settings: Settings) -> None:
        self.path = settings.sqlite_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_hit_at TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def key(provider: str, model: str, operation: str, payload: Any) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(
            b"\0".join([provider.encode(), model.encode(), operation.encode(), encoded])
        ).hexdigest()

    def get(self, cache_key: str) -> Any | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM provider_cache WHERE cache_key=?", (cache_key,)
            ).fetchone()
            if row:
                connection.execute(
                    """
                    UPDATE provider_cache SET hit_count=hit_count+1,last_hit_at=?
                    WHERE cache_key=?
                    """,
                    (datetime.now(UTC).isoformat(), cache_key),
                )
        return json.loads(row[0]) if row else None

    def put(
        self,
        cache_key: str,
        *,
        provider: str,
        model: str,
        operation: str,
        value: Any,
    ) -> None:
        timestamp = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO provider_cache(
                    cache_key,provider,model,operation,value,created_at,last_hit_at,hit_count
                ) VALUES(?,?,?,?,?,?,?,COALESCE(
                    (SELECT hit_count FROM provider_cache WHERE cache_key=?),0
                ))
                """,
                (
                    cache_key,
                    provider,
                    model,
                    operation,
                    json.dumps(value, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    cache_key,
                ),
            )
