from __future__ import annotations

import json
import random
import shutil
import sqlite3
import statistics
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models

from scholarrag.config import Settings
from scholarrag.storage.catalog import Catalog


class ScaleBenchmarkRunner:
    def __init__(self, *, paper_count: int = 3000, chunks_per_paper: int = 3) -> None:
        self.paper_count = paper_count
        self.chunks_per_paper = chunks_per_paper

    def run(self, output: Path) -> dict[str, Any]:
        root = Path(tempfile.mkdtemp(prefix="scholarrag-scale-"))
        settings = Settings(data_dir=root)
        Catalog(settings)
        setup_started = time.perf_counter()
        self._populate_sqlite(settings.sqlite_path)
        sqlite_setup = time.perf_counter() - setup_started
        catalog = Catalog(settings)
        queries = [f"method benchmark token{i % 97}" for i in range(200)]
        serial = self._measure(lambda query: catalog.lexical_search(query, limit=10), queries)
        concurrent_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=8) as pool:
            concurrent_results = list(pool.map(catalog.lexical_search, queries))
        concurrent_seconds = time.perf_counter() - concurrent_started
        qdrant = self._benchmark_qdrant(root / "qdrant-scale")
        backup = root / "catalog-backup.db"
        shutil.copy2(settings.sqlite_path, backup)
        with sqlite3.connect(backup) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            restored_documents = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        report = {
            "schema_version": "1.0",
            "kind": "synthetic_capacity_acceptance",
            "paper_count": self.paper_count,
            "chunk_count": self.paper_count * self.chunks_per_paper,
            "sqlite": {
                "setup_seconds": round(sqlite_setup, 3),
                "bytes": settings.sqlite_path.stat().st_size,
                "serial_fts_ms": serial,
                "concurrent_queries": len(concurrent_results),
                "concurrency": 8,
                "concurrent_total_seconds": round(concurrent_seconds, 3),
                "backup_integrity": integrity,
                "restored_document_count": restored_documents,
            },
            "qdrant": qdrant,
            "limitations": [
                "Synthetic text and 128-dimensional vectors measure engineering capacity, not RAG quality.",
                "Provider API throughput and paid parsing latency are excluded.",
            ],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(root)
        return {**report, "report_path": str(output.resolve())}

    def _populate_sqlite(self, path: Path) -> None:
        with sqlite3.connect(path) as connection:
            documents = []
            parses = []
            chunks = []
            fts = []
            for paper_index in range(self.paper_count):
                document_id = f"doc_scale_{paper_index:06d}"
                parse_id = f"parse_scale_{paper_index:06d}"
                documents.append(
                    (
                        document_id,
                        f"Synthetic paper {paper_index}",
                        f"paper-{paper_index}.pdf",
                        f"{paper_index:064x}"[-64:],
                        f"/immutable/paper-{paper_index}.pdf",
                        "2026-01-01T00:00:00+00:00",
                    )
                )
                parses.append(
                    (
                        parse_id,
                        document_id,
                        "scale",
                        "synthetic",
                        f"/artifacts/{document_id}/{parse_id}",
                        1,
                        "2026-01-01T00:00:00+00:00",
                    )
                )
                for chunk_index in range(self.chunks_per_paper):
                    chunk_id = f"chk_scale_{paper_index:06d}_{chunk_index}"
                    text = (
                        f"Synthetic method benchmark token{paper_index % 97} paper {paper_index} "
                        f"section {chunk_index} reproducible evaluation evidence."
                    )
                    chunks.append(
                        (
                            chunk_id,
                            parse_id,
                            document_id,
                            text,
                            text,
                            "[1]",
                            f'["block-{paper_index}-{chunk_index}"]',
                            '["Methods"]',
                            "[]",
                            "{}",
                            "atomic",
                            None,
                        )
                    )
                    fts.append((chunk_id, document_id, text))
            connection.executemany("INSERT INTO documents VALUES(?,?,?,?,?,?)", documents)
            connection.executemany("INSERT INTO parses VALUES(?,?,?,?,?,?,?)", parses)
            connection.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", chunks)
            connection.executemany(
                "INSERT INTO chunks_fts(chunk_id,document_id,text) VALUES(?,?,?)", fts
            )

    def _benchmark_qdrant(self, path: Path) -> dict[str, Any]:
        client = QdrantClient(path=str(path))
        dimension = 128
        collection = "scale"
        client.create_collection(
            collection,
            vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
        )
        rng = random.Random(7)
        started = time.perf_counter()
        total = self.paper_count * self.chunks_per_paper
        for start in range(0, total, 512):
            points = []
            for index in range(start, min(start + 512, total)):
                points.append(
                    models.PointStruct(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"scale:{index}")),
                        vector=[rng.random() for _ in range(dimension)],
                        payload={"document_id": f"doc_scale_{index // self.chunks_per_paper:06d}"},
                    )
                )
            client.upsert(collection, points=points, wait=True)
        index_seconds = time.perf_counter() - started
        query = [rng.random() for _ in range(dimension)]
        latencies = []
        for _ in range(50):
            tick = time.perf_counter()
            client.query_points(collection, query=query, limit=10)
            latencies.append((time.perf_counter() - tick) * 1000)
        client.close()
        return {
            "vector_count": total,
            "dimensions": dimension,
            "index_seconds": round(index_seconds, 3),
            "query_ms": self._distribution(latencies),
        }

    @staticmethod
    def _measure(function: Any, values: list[str]) -> dict[str, float]:
        latencies = []
        for value in values:
            started = time.perf_counter()
            function(value)
            latencies.append((time.perf_counter() - started) * 1000)
        return ScaleBenchmarkRunner._distribution(latencies)

    @staticmethod
    def _distribution(values: list[float]) -> dict[str, float]:
        ordered = sorted(values)
        return {
            "p50": round(statistics.median(ordered), 3),
            "p95": round(ordered[int(len(ordered) * 0.95) - 1], 3),
            "mean": round(statistics.fmean(ordered), 3),
        }
