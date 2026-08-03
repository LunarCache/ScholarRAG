from __future__ import annotations

import asyncio
import atexit
import base64
import mimetypes
import threading
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models

from scholarrag.config import Settings
from scholarrag.domain.models import BlockKind, CanonicalDocument, Chunk, ContentBlock
from scholarrag.providers.siliconflow import SiliconFlowClient

_EMBEDDED_CLIENTS: dict[str, QdrantClient] = {}
_EMBEDDED_CLIENTS_LOCK = threading.Lock()


def _embedded_client(path: str) -> QdrantClient:
    """One local client per storage path avoids Qdrant's multi-instance lock race."""
    with _EMBEDDED_CLIENTS_LOCK:
        client = _EMBEDDED_CLIENTS.get(path)
        if client is None:
            client = QdrantClient(path=path)
            _EMBEDDED_CLIENTS[path] = client
        return client


def close_embedded_clients() -> None:
    with _EMBEDDED_CLIENTS_LOCK:
        clients = list(_EMBEDDED_CLIENTS.values())
        _EMBEDDED_CLIENTS.clear()
    for client in clients:
        client.close()


atexit.register(close_embedded_clients)


def _client_for(settings: Settings) -> QdrantClient:
    if settings.qdrant_url:
        return QdrantClient(
            url=settings.qdrant_url,
            api_key=(
                settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
            ),
        )
    return _embedded_client(str(settings.vector_dir.resolve()))


def _close_if_remote(settings: Settings, client: QdrantClient) -> None:
    if settings.qdrant_url:
        client.close()


class VectorIndex:
    # v2 deliberately separates the unified VL vectors from the legacy text-model collection.
    collection = "scholarrag_chunks_vl_v2"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _client(self) -> QdrantClient:
        return _client_for(self.settings)

    async def exists(self) -> bool:
        return await asyncio.to_thread(self._exists)

    def _exists(self) -> bool:
        client = self._client()
        try:
            return client.collection_exists(self.collection)
        finally:
            _close_if_remote(self.settings, client)

    async def index(self, chunks: list[Chunk], *, batch_size: int = 16) -> int:
        chunks = [chunk for chunk in chunks if chunk.level != "parent"]
        if not chunks:
            return 0
        provider = SiliconFlowClient(self.settings)
        indexed = 0
        try:
            for start in range(0, len(chunks), batch_size):
                batch = chunks[start : start + batch_size]
                response = await provider.embeddings(
                    [chunk.contextual_text for chunk in batch],
                    model=self.settings.unified_embedding_model,
                    dimensions=self.settings.embedding_dimensions,
                )
                vectors = [row["embedding"] for row in response.data["data"]]
                await asyncio.to_thread(self._upsert, batch, vectors)
                indexed += len(batch)
        finally:
            await provider.close()
        return indexed

    def _upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        client = self._client()
        try:
            dimension = len(vectors[0])
            if not client.collection_exists(self.collection):
                client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(
                        size=dimension, distance=models.Distance.COSINE
                    ),
                )
            points = [
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, chunk.id)),
                    vector=vector,
                    payload={
                        "chunk_id": chunk.id,
                        "document_id": chunk.document_id,
                        "parse_id": chunk.parse_id,
                        "page_numbers": chunk.page_numbers,
                        "chunk_level": chunk.level,
                        "parent_chunk_id": chunk.parent_chunk_id,
                    },
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
            client.upsert(collection_name=self.collection, points=points, wait=True)
        finally:
            _close_if_remote(self.settings, client)

    async def search(
        self,
        vector: list[float],
        *,
        document_ids: list[str] | None = None,
        parse_ids: list[str] | None = None,
        limit: int = 30,
    ) -> list[tuple[str, float]]:
        return await asyncio.to_thread(self._search, vector, document_ids, parse_ids, limit)

    def _search(
        self,
        vector: list[float],
        document_ids: list[str] | None,
        parse_ids: list[str] | None,
        limit: int,
    ) -> list[tuple[str, float]]:
        client = self._client()
        try:
            if not client.collection_exists(self.collection):
                return []
            conditions = []
            if document_ids:
                conditions.append(
                    models.FieldCondition(
                        key="document_id", match=models.MatchAny(any=document_ids)
                    )
                )
            if parse_ids:
                conditions.append(
                    models.FieldCondition(key="parse_id", match=models.MatchAny(any=parse_ids))
                )
            query_filter = models.Filter(must=conditions) if conditions else None
            result = client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            return [(str(point.payload["chunk_id"]), float(point.score)) for point in result.points]
        finally:
            _close_if_remote(self.settings, client)

    async def delete_parse_ids(self, parse_ids: list[str]) -> int:
        if not parse_ids:
            return 0
        return await asyncio.to_thread(self._delete_parse_ids, parse_ids)

    def _delete_parse_ids(self, parse_ids: list[str]) -> int:
        client = self._client()
        try:
            if not client.collection_exists(self.collection):
                return 0
            client.delete(
                collection_name=self.collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="parse_id", match=models.MatchAny(any=parse_ids)
                            )
                        ]
                    )
                ),
                wait=True,
            )
            return len(parse_ids)
        finally:
            _close_if_remote(self.settings, client)


class VisualIndex:
    collection = "scholarrag_visuals"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dimensions = settings.embedding_dimensions

    def _client(self) -> QdrantClient:
        return _client_for(self.settings)

    async def exists(self) -> bool:
        return await asyncio.to_thread(self._exists)

    def _exists(self) -> bool:
        client = self._client()
        try:
            return client.collection_exists(self.collection)
        finally:
            _close_if_remote(self.settings, client)

    async def index(
        self, document: CanonicalDocument, chunks: list[Chunk], *, batch_size: int = 8
    ) -> int:
        chunk_by_block = {block_id: chunk.id for chunk in chunks for block_id in chunk.block_ids}
        blocks = [
            block
            for block in document.blocks
            if block.kind in {BlockKind.FIGURE, BlockKind.TABLE}
            and block.image_path
            and Path(block.image_path).is_file()
            and block.id in chunk_by_block
        ]
        if not blocks:
            return 0
        provider = SiliconFlowClient(self.settings)
        indexed = 0
        try:
            for start in range(0, len(blocks), batch_size):
                batch = blocks[start : start + batch_size]
                inputs = [
                    {"image": self._data_url(Path(block.image_path or ""))} for block in batch
                ]
                response = await provider.multimodal_embeddings(inputs, dimensions=self.dimensions)
                vectors = [row["embedding"] for row in response.data["data"]]
                await asyncio.to_thread(
                    self._upsert, batch, vectors, chunk_by_block, document.parse_id
                )
                indexed += len(batch)
        finally:
            await provider.close()
        return indexed

    def _upsert(
        self,
        blocks: list[ContentBlock],
        vectors: list[list[float]],
        chunk_by_block: dict[str, str],
        parse_id: str,
    ) -> None:
        client = self._client()
        try:
            if not client.collection_exists(self.collection):
                client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(
                        size=self.dimensions, distance=models.Distance.COSINE
                    ),
                )
            points = [
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"visual:{block.id}")),
                    vector=vector,
                    payload={
                        "chunk_id": chunk_by_block[block.id],
                        "block_id": block.id,
                        "document_id": block.source.document_id,
                        "parse_id": parse_id,
                        "page_number": block.source.page_number,
                        "kind": block.kind.value,
                    },
                )
                for block, vector in zip(blocks, vectors, strict=True)
            ]
            client.upsert(collection_name=self.collection, points=points, wait=True)
        finally:
            _close_if_remote(self.settings, client)

    async def search(
        self,
        vector: list[float],
        *,
        document_ids: list[str] | None = None,
        parse_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[tuple[str, float]]:
        return await asyncio.to_thread(self._search, vector, document_ids, parse_ids, limit)

    def _search(
        self,
        vector: list[float],
        document_ids: list[str] | None,
        parse_ids: list[str] | None,
        limit: int,
    ) -> list[tuple[str, float]]:
        client = self._client()
        try:
            if not client.collection_exists(self.collection):
                return []
            conditions = []
            if document_ids:
                conditions.append(
                    models.FieldCondition(
                        key="document_id", match=models.MatchAny(any=document_ids)
                    )
                )
            if parse_ids:
                conditions.append(
                    models.FieldCondition(key="parse_id", match=models.MatchAny(any=parse_ids))
                )
            query_filter = models.Filter(must=conditions) if conditions else None
            result = client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            best_by_chunk: dict[str, float] = {}
            for point in result.points:
                chunk_id = str(point.payload["chunk_id"])
                best_by_chunk[chunk_id] = max(
                    best_by_chunk.get(chunk_id, float("-inf")), float(point.score)
                )
            return sorted(best_by_chunk.items(), key=lambda item: item[1], reverse=True)
        finally:
            _close_if_remote(self.settings, client)

    async def delete_parse_ids(self, parse_ids: list[str]) -> int:
        if not parse_ids:
            return 0
        return await asyncio.to_thread(self._delete_parse_ids, parse_ids)

    def _delete_parse_ids(self, parse_ids: list[str]) -> int:
        client = self._client()
        try:
            if not client.collection_exists(self.collection):
                return 0
            client.delete(
                collection_name=self.collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="parse_id", match=models.MatchAny(any=parse_ids)
                            )
                        ]
                    )
                ),
                wait=True,
            )
            return len(parse_ids)
        finally:
            _close_if_remote(self.settings, client)

    @staticmethod
    def _data_url(path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"
