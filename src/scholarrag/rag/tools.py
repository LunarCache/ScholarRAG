"""Tools exposed to the tool-calling agentic RAG loop.

Each tool is an OpenAI-compatible function spec plus an async handler.
Handlers return JSON-serializable dicts; the tool agent executes them,
streams their progress as trace events, and feeds results back to the
chat model as ``role="tool"`` messages.

Evidence numbering: the ``retrieve`` / ``read_chunk`` tools register every
returned chunk in an :class:`EvidenceRegistry`. The assigned ``[C#]`` index
is written back into the tool result so the model cites the same numbers in
its final answer, which the citation extractor then maps back to chunks.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scholarrag.authorization import current_guard
from scholarrag.config import Settings
from scholarrag.domain.access import AccessContext, AccessDenied
from scholarrag.domain.models import BlockKind, Chunk, RetrievedChunk, VisualEvidence
from scholarrag.rag.scope import DocumentScope
from scholarrag.retrieval.service import RetrievalService
from scholarrag.storage.catalog import Catalog
from scholarrag.storage.identity import IdentityStore

_PREVIEW_CHARS = 500
_FULL_CHUNK_CHARS = 6_000


class EvidenceRegistry:
    """Assigns stable ``[C#]`` indices to chunks surfaced by tools."""

    def __init__(self) -> None:
        self._pool: list[RetrievedChunk] = []
        self._by_chunk_id: dict[str, int] = {}

    def register(self, item: RetrievedChunk) -> int:
        existing = self._by_chunk_id.get(item.chunk.id)
        if existing is not None:
            return existing
        index = len(self._pool) + 1
        self._pool.append(item)
        self._by_chunk_id[item.chunk.id] = index
        return index

    def get(self, chunk_id: str) -> RetrievedChunk | None:
        index = self._by_chunk_id.get(chunk_id)
        if index is None:
            return None
        return self._pool[index - 1]

    @property
    def pool(self) -> list[RetrievedChunk]:
        return list(self._pool)

    def __len__(self) -> int:
        return len(self._pool)


class ToolRuntime:
    """Shared dependencies handed to every tool handler."""

    def __init__(
        self,
        settings: Settings,
        registry: EvidenceRegistry,
        *,
        document_ids: list[str] | None = None,
        top_k: int = 8,
        authorized_document_ids: list[str] | None = None,
        access_context: AccessContext | None = None,
        identities: IdentityStore | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.catalog = Catalog(settings)
        self.retrieval = RetrievalService(settings)
        self.default_document_ids = document_ids
        self.scope = DocumentScope.intersect(document_ids, authorized_document_ids)
        self.default_top_k = top_k
        if settings.auth_mode == "session" and access_context is None:
            guard = current_guard(settings)
            access_context, identities = guard.context, guard.identities
            self.scope = DocumentScope.intersect(self.scope.resolve(), list(guard.document_ids))
        if (access_context is None) != (identities is None):
            raise ValueError("Access context and identity store must be supplied together")
        if settings.auth_mode == "session" and access_context is None:
            raise AccessDenied("Session mode requires a server-created access context")
        self.access_context = access_context
        self.identities = identities
        if access_context is not None:
            self.scope = DocumentScope.intersect(
                self.scope.resolve(), identities.document_ids(access_context)
            )
        self._initial_scope = self.scope

    def refresh_scope(self) -> None:
        if self.access_context is not None:
            self.scope = DocumentScope.intersect(
                self._initial_scope.resolve(), self.identities.document_ids(self.access_context)
            )


ToolHandler = Callable[[ToolRuntime, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _document_summary(doc: dict[str, Any], settings: Settings) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "document_id": doc["document_id"],
        "title": doc.get("title"),
        "source_filename": doc.get("source_filename"),
        "chunk_count": doc.get("chunk_count"),
    }
    manifest = Path(str(doc["artifact_dir"])) / "manifest.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            summary["page_count"] = payload.get("page_count")
            quality = payload.get("quality") or {}
            summary["quality"] = {
                "score": quality.get("score"),
                "decision": quality.get("decision"),
            }
        except (OSError, ValueError):
            pass
    return summary


def _canonical_path(runtime: ToolRuntime, parse_id: str) -> Path | None:
    record = runtime.catalog.get_parse(parse_id)
    if record is None:
        return None
    return Path(str(record["artifact_dir"])) / "canonical" / "document.json"


async def _handle_list_papers(runtime: ToolRuntime, args: dict[str, Any]) -> dict[str, Any]:
    runtime.refresh_scope()
    query = str(args.get("query") or "").strip().casefold()
    limit = int(args.get("limit") or 20)
    documents = runtime.catalog.list_documents(runtime.scope.resolve())
    if query:
        documents = [
            doc
            for doc in documents
            if query in str(doc.get("title") or "").casefold()
            or query in str(doc.get("source_filename") or "").casefold()
        ]
    documents = documents[: max(1, min(limit, 50))]
    return {
        "total": len(documents),
        "papers": [_document_summary(doc, runtime.settings) for doc in documents],
        "note": "通过 document_id 精确指定论文范围；标题可用于模糊查找。",
    }


async def _handle_get_paper(runtime: ToolRuntime, args: dict[str, Any]) -> dict[str, Any]:
    runtime.refresh_scope()
    document_id = str(args.get("document_id") or "")
    if not runtime.scope.allows(document_id):
        return {"error": "论文不可用或不在当前范围中", "paper": None}
    doc = runtime.catalog.get_document(document_id)
    if doc is None:
        return {"error": f"论文 {document_id!r} 不存在", "papers": None}
    summary = _document_summary(doc, runtime.settings)
    metadata = runtime.catalog.get_metadata(document_id) or {}
    summary["metadata"] = {
        key: metadata.get(key)
        for key in ("title", "authors", "abstract", "methods", "datasets", "metrics")
        if metadata.get(key) is not None
    }
    headings: list[dict[str, Any]] = []
    canonical = _canonical_path(runtime, doc["parse_id"])
    if canonical and canonical.is_file():
        try:
            payload = json.loads(canonical.read_text(encoding="utf-8"))
            seen: set[str] = set()
            for block in payload.get("blocks", []):
                if block.get("kind") != BlockKind.HEADING.value:
                    continue
                path = tuple(block.get("section_path") or [])
                text = str(block.get("text") or "").strip()
                if not text or (path, text) in seen:
                    continue
                seen.add((path, text))
                headings.append(
                    {
                        "section": list(path),
                        "heading": text,
                        "page": block.get("source", {}).get("page_number"),
                    }
                )
        except (OSError, ValueError, TypeError):
            headings = []
    summary["headings"] = headings[:60]
    return {"paper": summary}


async def _handle_retrieve(runtime: ToolRuntime, args: dict[str, Any]) -> dict[str, Any]:
    runtime.refresh_scope()
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query 不能为空", "candidates": []}
    document_ids = runtime.scope.resolve(args.get("document_ids"))
    if document_ids == []:
        return {"query": query, "candidate_count": 0, "candidates": []}
    top_k = max(1, min(int(args.get("top_k") or runtime.default_top_k), 12))
    results, _debug = await runtime.retrieval.retrieve(
        query,
        document_ids=document_ids,
        top_k=top_k,
        events=None,
    )
    candidates: list[dict[str, Any]] = []
    runtime.refresh_scope()
    for item in results:
        if not runtime.scope.allows(item.chunk.document_id) or (
            document_ids is not None and item.chunk.document_id not in document_ids
        ):
            continue
        index = runtime.registry.register(item)
        chunk = item.chunk
        candidates.append(
            {
                "evidence_index": index,
                "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "page_number": chunk.page_numbers[0] if chunk.page_numbers else None,
                "section_path": chunk.section_path,
                "score": round(item.score, 4),
                "text_preview": chunk.contextual_text[:_PREVIEW_CHARS],
                "has_visual": bool(chunk.visual_evidence),
            }
        )
    return {
        "query": query,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "usage": "在最终回答中使用 [C#] 引用候选证据编号，例如 [C1]。",
    }


async def _handle_read_chunk(runtime: ToolRuntime, args: dict[str, Any]) -> dict[str, Any]:
    runtime.refresh_scope()
    chunk_id = str(args.get("chunk_id") or "")
    registered = runtime.registry.get(chunk_id)
    if registered is not None:
        if not runtime.scope.allows(registered.chunk.document_id):
            return {"error": "Chunk 不可用或不在当前范围中", "chunk": None}
        index = runtime.registry.register(registered)
        chunk = registered.chunk
    else:
        loaded = runtime.catalog.chunks_by_ids([chunk_id], document_ids=runtime.scope.resolve())
        chunk = loaded.get(chunk_id)
        if chunk is None:
            return {"error": f"Chunk {chunk_id!r} 不存在或不在 active parse 中", "chunk": None}
        if not runtime.scope.allows(chunk.document_id):
            return {"error": "Chunk 不可用或不在当前范围中", "chunk": None}
        item = RetrievedChunk(chunk=chunk, score=0.0)
        index = runtime.registry.register(item)
    visuals = _attach_visuals(runtime, chunk)
    result: dict[str, Any] = {
        "evidence_index": index,
        "chunk_id": chunk.id,
        "document_id": chunk.document_id,
        "page_numbers": chunk.page_numbers,
        "section_path": chunk.section_path,
        "text": chunk.contextual_text[:_FULL_CHUNK_CHARS],
        "truncated": len(chunk.contextual_text) > _FULL_CHUNK_CHARS,
        "visuals": [
            {
                "kind": visual.kind.value,
                "page_number": visual.page_number,
                "caption": visual.caption[:200],
            }
            for visual in visuals
        ],
        "usage": "在最终回答中使用 [C#] 引用该证据编号。",
    }
    return result


def _attach_visuals(runtime: ToolRuntime, chunk: Chunk) -> list[VisualEvidence]:
    """Resolve figure/table images for a chunk and attach them for synthesis.

    The SQLite chunk row does not persist ``visual_evidence``; image paths
    are re-resolved from the canonical block map, mirroring the rerank path.
    """
    if chunk.visual_evidence:
        return chunk.visual_evidence
    image_paths, _diagnostics = runtime.retrieval._chunk_image_paths([chunk])
    attached: list[VisualEvidence] = []
    for raw_path in image_paths.get(chunk.id, []):
        resolved = Path(raw_path)
        if not resolved.is_file():
            continue
        attached.append(
            VisualEvidence(
                kind=BlockKind.FIGURE,
                image_path=str(resolved),
                page_number=chunk.page_numbers[0] if chunk.page_numbers else 1,
                caption=(
                    f"{chunk.section_path[-1] if chunk.section_path else '图'}（{resolved.name}）"
                ),
            )
        )
    chunk.visual_evidence = attached
    return attached


def build_tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_papers",
            description=(
                "列出当前论文库中的论文（可按标题关键词过滤）。当你需要了解可检索范围、"
                "确认论文是否在库中，或不确定论文的 document_id 时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "标题关键词（可选），为空时返回全部论文",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回的论文数，默认 20，最大 50",
                    },
                },
            },
            handler=_handle_list_papers,
        ),
        ToolSpec(
            name="get_paper",
            description=(
                "获取单篇论文的元数据与章节大纲：标题、作者、摘要、方法/数据集/指标标签，"
                "以及按页码排序的章节标题列表。在对比论文、按章节定位内容或撰写综述前使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "论文的 document_id，来自 list_papers",
                    }
                },
                "required": ["document_id"],
            },
            handler=_handle_get_paper,
        ),
        ToolSpec(
            name="retrieve",
            description=(
                "在论文库中执行混合检索（语义向量 + 关键词 + 视觉向量 + 重排），返回候选证据"
                "及其 [C#] 编号、页码、章节与文本预览。这是获取证据的主要工具；检索结果不够"
                "具体时再用 read_chunk 读取全文。最终回答必须用 [C#] 引用这些证据。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索查询，应聚焦单一事实/方法/图表，可中英混写",
                    },
                    "document_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "限定检索的论文范围（可选，默认全部已选论文）",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回候选数，默认 6，最大 12",
                    },
                },
                "required": ["query"],
            },
            handler=_handle_retrieve,
        ),
        ToolSpec(
            name="read_chunk",
            description=(
                "读取某个证据 Chunk 的完整正文（含页码、章节和图表说明）。当 retrieve 的文本"
                "预览不足以支撑结论、需要精确公式/数值/图注，或需要确认图片证据时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "chunk_id": {
                        "type": "string",
                        "description": "候选证据的 chunk_id，来自 retrieve 或之前的结果",
                    }
                },
                "required": ["chunk_id"],
            },
            handler=_handle_read_chunk,
        ),
    ]
