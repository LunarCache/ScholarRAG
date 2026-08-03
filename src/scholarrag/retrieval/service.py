from __future__ import annotations

import asyncio
import base64
import mimetypes
from collections import defaultdict
from pathlib import Path

from scholarrag.config import Settings
from scholarrag.domain.models import CanonicalDocument, Chunk, RetrievedChunk
from scholarrag.observability import get_tracer
from scholarrag.providers.siliconflow import SiliconFlowClient
from scholarrag.rag.runs import EventSink, PipelineTracer
from scholarrag.retrieval.index import VectorIndex, VisualIndex
from scholarrag.storage.catalog import Catalog


class RetrievalService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.catalog = Catalog(settings)
        self.vector_index = VectorIndex(settings)
        self.visual_index = VisualIndex(settings)

    async def retrieve(
        self,
        query: str,
        *,
        document_ids: list[str] | None = None,
        top_k: int = 8,
        events: EventSink | None = None,
    ) -> tuple[list[RetrievedChunk], dict[str, object]]:
        with get_tracer().start_as_current_span("rag.retrieve") as span:
            results, debug = await self._retrieve(
                query,
                document_ids=document_ids,
                top_k=top_k,
                events=events,
            )
            span.set_attribute("retrieval.result_count", len(results))
            span.set_attribute("retrieval.dense_candidates", int(debug.get("dense_candidates", 0)))
            span.set_attribute(
                "retrieval.lexical_candidates", int(debug.get("lexical_candidates", 0))
            )
            for key in ("embedding_trace_id", "rerank_trace_id"):
                if debug.get(key):
                    span.set_attribute(f"provider.{key}", str(debug[key]))
            return results, debug

    async def _retrieve(
        self,
        query: str,
        *,
        document_ids: list[str] | None,
        top_k: int,
        events: EventSink | None,
    ) -> tuple[list[RetrievedChunk], dict[str, object]]:
        provider = SiliconFlowClient(self.settings)
        pipeline = PipelineTracer(events)
        try:
            async with pipeline.stage(
                "scope.resolve",
                "确定论文范围",
                component="catalog",
                input={"document_ids": document_ids or []},
            ) as stage:
                active_parse_ids = self.catalog.active_parse_ids(document_ids)
                stage.complete(
                    output={
                        "document_count": len(document_ids or []),
                        "active_parse_count": len(active_parse_ids),
                        "active_parse_ids": active_parse_ids,
                    },
                    summary=f"{len(active_parse_ids)} 个 active parse",
                )
            if not active_parse_ids:
                for stage_id, label in (
                    ("query.embedding.vl", "统一 VL Query Embedding"),
                    ("retrieval.dense", "VL 文本块召回"),
                    ("retrieval.lexical", "关键词召回"),
                    ("retrieval.visual", "视觉向量召回"),
                    ("retrieval.fusion", "RRF 多路融合"),
                    ("retrieval.chunk_load", "读取候选 Chunk"),
                    ("retrieval.rerank", "Qwen3-VL Reranker"),
                ):
                    await pipeline.skip(stage_id, label, summary="没有可检索的 active parse")
                return [], {
                    "dense_candidates": 0,
                    "lexical_candidates": 0,
                    "visual_candidates": 0,
                    "active_parse_ids": [],
                }

            async with pipeline.stage(
                "query.embedding.vl",
                "统一 VL Query Embedding",
                component="siliconflow",
                model=self.settings.unified_embedding_model,
                input={"query_chars": len(query)},
            ) as stage:
                response = await provider.multimodal_embeddings(
                    [query], dimensions=self.settings.embedding_dimensions
                )
                query_vector = response.data["data"][0]["embedding"]
                embedding_trace_id = response.trace_id
                stage.complete(
                    output={
                        "dimensions": len(query_vector),
                        "usage": response.data.get("usage") or {},
                        "consumers": ["retrieval.dense", "retrieval.visual"],
                    },
                    trace={"provider_trace_id": response.trace_id},
                    summary=f"{len(query_vector)} 维共享向量 → 文本块/图片",
                )

            async def dense_branch():
                dense_limit = max(30, top_k * 4)
                async with pipeline.stage(
                    "retrieval.dense",
                    "VL 文本块召回",
                    parent_stage_id="retrieval",
                    component="qdrant",
                    input={
                        "limit": dense_limit,
                        "active_parse_count": len(active_parse_ids),
                    },
                ) as stage:
                    ranking = await self.vector_index.search(
                        query_vector,
                        document_ids=document_ids,
                        parse_ids=active_parse_ids,
                        limit=dense_limit,
                    )
                    stage.complete(
                        output={
                            "candidate_count": len(ranking),
                            "top_candidates": self._ranking_preview(ranking),
                        },
                        summary=f"{len(ranking)} 个候选",
                    )
                return ranking

            async def visual_branch():
                if not await self.visual_index.exists():
                    await pipeline.skip(
                        "retrieval.visual",
                        "视觉向量召回",
                        parent_stage_id="retrieval",
                        component="qdrant",
                        summary="当前没有视觉索引",
                    )
                    return []
                visual_limit = max(20, top_k * 3)
                async with pipeline.stage(
                    "retrieval.visual",
                    "视觉向量召回",
                    parent_stage_id="retrieval",
                    component="qdrant",
                    input={"limit": visual_limit},
                ) as stage:
                    ranking = await self.visual_index.search(
                        query_vector,
                        document_ids=document_ids,
                        parse_ids=active_parse_ids,
                        limit=visual_limit,
                    )
                    stage.complete(
                        output={
                            "candidate_count": len(ranking),
                            "top_candidates": self._ranking_preview(ranking),
                        },
                        summary=f"{len(ranking)} 个候选",
                    )
                return ranking

            async def lexical_branch():
                lexical_limit = max(30, top_k * 4)
                async with pipeline.stage(
                    "retrieval.lexical",
                    "关键词召回",
                    parent_stage_id="retrieval",
                    component="sqlite-fts5",
                    input={"limit": lexical_limit},
                ) as stage:
                    ranking = await asyncio.to_thread(
                        self.catalog.lexical_search,
                        query,
                        document_ids=document_ids,
                        limit=lexical_limit,
                    )
                    stage.complete(
                        output={
                            "candidate_count": len(ranking),
                            "top_candidates": self._ranking_preview(ranking),
                        },
                        summary=f"{len(ranking)} 个候选",
                    )
                return ranking

            (dense, visual, lexical) = await asyncio.gather(
                dense_branch(), visual_branch(), lexical_branch()
            )
            visual_trace_id = embedding_trace_id if visual else None

            async with pipeline.stage(
                "retrieval.fusion",
                "RRF 多路融合",
                parent_stage_id="retrieval",
                component="rrf",
                input={
                    "dense_candidates": len(dense),
                    "lexical_candidates": len(lexical),
                    "visual_candidates": len(visual),
                    "rrf_k": 60,
                },
            ) as stage:
                fused = self._rrf(dense, lexical, visual)
                stage.complete(
                    output={
                        "candidate_count": len(fused),
                        "top_candidates": self._ranking_preview(fused),
                    },
                    summary=f"融合为 {len(fused)} 个候选",
                )

            candidate_ids = [chunk_id for chunk_id, _ in fused[: max(20, top_k * 3)]]
            async with pipeline.stage(
                "retrieval.chunk_load",
                "读取候选 Chunk",
                parent_stage_id="retrieval",
                component="catalog",
                input={"candidate_count": len(candidate_ids)},
            ) as stage:
                chunks = self.catalog.chunks_by_ids(candidate_ids)
                candidate_ids = [chunk_id for chunk_id in candidate_ids if chunk_id in chunks]
                stage.complete(
                    output={"loaded_count": len(candidate_ids)},
                    summary=f"读取 {len(candidate_ids)} 个 Chunk",
                )
            if not candidate_ids:
                await pipeline.skip(
                    "retrieval.rerank",
                    "Qwen3-VL Reranker",
                    parent_stage_id="retrieval",
                    component="siliconflow",
                    model=self.settings.rerank_model,
                    summary="融合后没有可读取候选",
                )
                return [], {
                    "embedding_trace_id": embedding_trace_id,
                    "dense_candidates": len(dense),
                    "lexical_candidates": len(lexical),
                    "visual_candidates": len(visual),
                }

            rerank_documents, rerank_input_refs, image_diagnostics = (
                self._build_vl_rerank_documents(candidate_ids, chunks)
            )
            target_rerank_count = min(max(top_k * 3, top_k), len(candidate_ids))
            modality_counts = {
                "text": sum(ref["modality"] == "text" for ref in rerank_input_refs),
                "image": sum(ref["modality"] == "image" for ref in rerank_input_refs),
            }

            async with pipeline.stage(
                "retrieval.rerank",
                "Qwen3-VL Reranker",
                parent_stage_id="retrieval",
                component="siliconflow",
                model=self.settings.rerank_model,
                input={
                    "candidate_count": len(candidate_ids),
                    "rerank_input_count": len(rerank_documents),
                    "target_chunk_count": target_rerank_count,
                    "modality_counts": modality_counts,
                    "image_chunk_ids": sorted(
                        {
                            str(ref["chunk_id"])
                            for ref in rerank_input_refs
                            if ref["modality"] == "image"
                        }
                    ),
                    "image_diagnostics": image_diagnostics,
                },
            ) as stage:
                rerank_response = await provider.rerank(
                    query,
                    rerank_documents,
                    # Return every text/image input so duplicate modalities can be
                    # aggregated back to one auditable score per chunk.
                    top_n=len(rerank_documents),
                )
                dense_scores = dict(dense)
                lexical_scores = dict(lexical)
                results: list[RetrievedChunk] = []
                reranked_preview: list[dict[str, object]] = []
                best_by_chunk: dict[str, tuple[float, str]] = {}
                for row in rerank_response.data.get("results", []):
                    input_index = int(row["index"])
                    if not 0 <= input_index < len(rerank_input_refs):
                        continue
                    ref = rerank_input_refs[input_index]
                    chunk_id = str(ref["chunk_id"])
                    score = float(row["relevance_score"])
                    previous = best_by_chunk.get(chunk_id)
                    if previous is None or score > previous[0]:
                        best_by_chunk[chunk_id] = (score, str(ref["modality"]))
                ranked_chunks = sorted(
                    best_by_chunk.items(), key=lambda item: item[1][0], reverse=True
                )[:target_rerank_count]
                for rank, (chunk_id, (score, winning_modality)) in enumerate(
                    ranked_chunks, start=1
                ):
                    results.append(
                        RetrievedChunk(
                            chunk=chunks[chunk_id],
                            score=score,
                            dense_score=dense_scores.get(chunk_id),
                            lexical_score=lexical_scores.get(chunk_id),
                            rerank_score=score,
                        )
                    )
                    chunk = chunks[chunk_id]
                    source = chunk.sources[0] if chunk.sources else None
                    reranked_preview.append(
                        {
                            "rank": rank,
                            "chunk_id": chunk_id,
                            "score": score,
                            "winning_modality": winning_modality,
                            "document_id": chunk.document_id,
                            "page_number": source.page_number if source else None,
                            "bbox": source.bbox.model_dump(mode="json")
                            if source and source.bbox
                            else None,
                            "section_path": chunk.section_path,
                            "text_preview": chunk.text[:240],
                        }
                    )
                results = self._diversify(results, top_k=top_k)
                parent_ids = {
                    item.chunk.parent_chunk_id for item in results if item.chunk.parent_chunk_id
                }
                parents = self.catalog.chunks_by_ids(parent_ids)
                for item in results:
                    parent = parents.get(item.chunk.parent_chunk_id or "")
                    if parent:
                        item.chunk.metadata["parent_text"] = parent.text
                        item.chunk.metadata["parent_chunk_id"] = parent.id
                stage.complete(
                    output={
                        "candidate_count": len(candidate_ids),
                        "rerank_input_count": len(rerank_documents),
                        "modality_counts": modality_counts,
                        "image_diagnostics": image_diagnostics,
                        "returned_count": len(results),
                        "expanded_parent_count": len(parents),
                        "top_candidates": reranked_preview,
                        "usage": rerank_response.data.get("usage")
                        or rerank_response.data.get("meta")
                        or {},
                    },
                    trace={"provider_trace_id": rerank_response.trace_id},
                    summary=f"多样性约束后保留 {len(results)} 个证据候选",
                )
            return results, {
                "embedding_trace_id": embedding_trace_id,
                "rerank_trace_id": rerank_response.trace_id,
                "visual_embedding_trace_id": visual_trace_id,
                "dense_candidates": len(dense),
                "lexical_candidates": len(lexical),
                "visual_candidates": len(visual),
                "active_parse_ids": active_parse_ids,
                "fused_candidates": len(fused),
                "returned": len(results),
                "rankings": {
                    "dense": self._ranking_preview(dense),
                    "lexical": self._ranking_preview(lexical),
                    "visual": self._ranking_preview(visual),
                    "fused": self._ranking_preview(fused),
                    "reranked": reranked_preview,
                },
            }
        finally:
            await provider.close()

    def _build_vl_rerank_documents(
        self,
        candidate_ids: list[str],
        chunks: dict[str, Chunk],
    ) -> tuple[
        list[str | dict[str, str]],
        list[dict[str, str]],
        list[dict[str, str]],
    ]:
        """Build mixed text/image inputs while retaining a reversible chunk mapping."""
        image_paths, diagnostics = self._chunk_image_paths(
            [chunks[chunk_id] for chunk_id in candidate_ids]
        )
        documents: list[str | dict[str, str]] = []
        refs: list[dict[str, str]] = []
        for chunk_id in candidate_ids:
            documents.append(chunks[chunk_id].contextual_text)
            refs.append({"chunk_id": chunk_id, "modality": "text"})
            for image_path in image_paths.get(chunk_id, []):
                documents.append({"image": self._data_url(image_path)})
                refs.append({"chunk_id": chunk_id, "modality": "image"})
        return documents, refs, diagnostics

    def _chunk_image_paths(
        self, chunks: list[Chunk]
    ) -> tuple[dict[str, list[Path]], list[dict[str, str]]]:
        chunks_by_parse: dict[str, list[Chunk]] = defaultdict(list)
        for chunk in chunks:
            chunks_by_parse[chunk.parse_id].append(chunk)
        paths: dict[str, list[Path]] = defaultdict(list)
        diagnostics: list[dict[str, str]] = []
        artifacts_root = self.settings.artifacts_dir.resolve()
        for parse_id, parse_chunks in chunks_by_parse.items():
            record = self.catalog.get_parse(parse_id)
            if record is None:
                diagnostics.append({"parse_id": parse_id, "reason": "parse_not_found"})
                continue
            canonical_path = Path(str(record["artifact_dir"])) / "canonical" / "document.json"
            if not canonical_path.is_file():
                diagnostics.append(
                    {
                        "parse_id": parse_id,
                        "reason": "canonical_not_found",
                    }
                )
                continue
            document = CanonicalDocument.model_validate_json(
                canonical_path.read_text(encoding="utf-8")
            )
            image_by_block = {
                block.id: block.image_path for block in document.blocks if block.image_path
            }
            for chunk in parse_chunks:
                seen: set[Path] = set()
                for block_id in chunk.block_ids:
                    raw_path = image_by_block.get(block_id)
                    if not raw_path:
                        continue
                    resolved = self._resolve_artifact_image(raw_path, artifacts_root)
                    if resolved is None:
                        diagnostics.append(
                            {
                                "chunk_id": chunk.id,
                                "block_id": block_id,
                                "reason": "image_missing_or_outside_artifacts",
                            }
                        )
                    elif resolved not in seen:
                        paths[chunk.id].append(resolved)
                        seen.add(resolved)
        return dict(paths), diagnostics

    @staticmethod
    def _resolve_artifact_image(raw_path: str, artifacts_root: Path) -> Path | None:
        path = Path(raw_path)
        candidates = [path] if path.is_absolute() else [Path.cwd() / path]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_relative_to(artifacts_root) and resolved.is_file():
                return resolved
        return None

    @staticmethod
    def _data_url(path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _rrf(*rankings: list[tuple[str, float]], k: int = 60) -> list[tuple[str, float]]:
        scores: dict[str, float] = {}
        for results in rankings:
            for rank, (chunk_id, _) in enumerate(results, start=1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
        return sorted(scores.items(), key=lambda item: item[1], reverse=True)

    @staticmethod
    def _ranking_preview(
        ranking: list[tuple[str, float]], *, limit: int = 20
    ) -> list[dict[str, object]]:
        return [
            {"rank": rank, "chunk_id": chunk_id, "score": score}
            for rank, (chunk_id, score) in enumerate(ranking[:limit], start=1)
        ]

    @staticmethod
    def _diversify(
        results: list[RetrievedChunk],
        *,
        top_k: int,
        max_per_document: int | None = None,
        max_per_section: int = 2,
    ) -> list[RetrievedChunk]:
        if not results:
            return []
        document_count = len({item.chunk.document_id for item in results})
        document_quota = max_per_document or (
            top_k if document_count <= 1 else max(2, (top_k + 1) // 2)
        )
        selected: list[RetrievedChunk] = []
        deferred: list[RetrievedChunk] = []
        document_counts: dict[str, int] = {}
        section_counts: dict[tuple[str, tuple[str, ...]], int] = {}
        seen_blocks: set[str] = set()
        for item in results:
            document_id = item.chunk.document_id
            section_key = (document_id, tuple(item.chunk.section_path))
            duplicate = bool(seen_blocks & set(item.chunk.block_ids))
            if (
                document_counts.get(document_id, 0) >= document_quota
                or section_counts.get(section_key, 0) >= max_per_section
                or duplicate
            ):
                deferred.append(item)
                continue
            selected.append(item)
            document_counts[document_id] = document_counts.get(document_id, 0) + 1
            section_counts[section_key] = section_counts.get(section_key, 0) + 1
            seen_blocks.update(item.chunk.block_ids)
            if len(selected) == top_k:
                return selected
        for item in deferred:
            if item not in selected:
                selected.append(item)
            if len(selected) == top_k:
                break
        return selected
