from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer

from scholarrag.benchmark.parsers import ParserBenchmarkRunner
from scholarrag.benchmark.scale import ScaleBenchmarkRunner
from scholarrag.cli_auth import app as auth_app
from scholarrag.config import get_settings
from scholarrag.domain.models import CanonicalDocument, Chunk
from scholarrag.evaluation.dataset import (
    EasyDatasetContextExporter,
    EvaluationCandidateGenerator,
    EvaluationDatasetLinker,
)
from scholarrag.evaluation.easydataset import EasyDatasetAdapter
from scholarrag.evaluation.human_review import HumanReviewService
from scholarrag.evaluation.parser_gold import ParserGoldPreparer
from scholarrag.evaluation.service import RagasEvaluator, load_cases
from scholarrag.ingestion.heading_repair import HeadingRepairService
from scholarrag.ingestion.quality import ParseQualityGate
from scholarrag.ingestion.review import ParseReviewService
from scholarrag.ingestion.service import IngestionService
from scholarrag.maintenance import BackupService
from scholarrag.observability import Telemetry
from scholarrag.providers.mineru import MinerUClient
from scholarrag.providers.paddleocr import PaddleOCRAPIClient
from scholarrag.providers.siliconflow import SiliconFlowClient
from scholarrag.rag.agent import AgenticRAGService
from scholarrag.retrieval.index import VectorIndex, VisualIndex
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog

app = typer.Typer(no_args_is_help=True)
app.add_typer(auth_app, name="auth")
PDF_ARGUMENT = typer.Argument(..., exists=True, dir_okay=False, readable=True)
WAIT_OPTION = typer.Option(True, help="Wait until parsing completes.")
DOCUMENT_IDS_OPTION = typer.Option(None, "--document-id", "-d")
EVALUATION_DATASET_ARGUMENT = typer.Argument(..., exists=True, dir_okay=False, readable=True)
EVALUATION_TARGET_ARGUMENT = typer.Argument(..., dir_okay=False)
PAGE_RANGES_OPTION = typer.Option(None, help="Pages accepted by the provider, e.g. 1-8 or 1,3,5.")
MINERU_PARSE_DIR_OPTION = typer.Option(
    None, exists=True, file_okay=False, readable=True, help="Completed MinerU parse to reuse."
)
BENCHMARK_RUN_ARGUMENT = typer.Argument(..., exists=True, file_okay=False, readable=True)
PARSER_GOLD_MANIFEST_ARGUMENT = typer.Argument(..., exists=True, dir_okay=False, readable=True)
PAPER_DIR_OPTION = typer.Option(Path("paper"), exists=True, file_okay=False, readable=True)
CANONICAL_ARGUMENT = typer.Argument(..., exists=True, dir_okay=False, readable=True)
SOURCE_PDF_OPTION = typer.Option(
    None, exists=True, dir_okay=False, readable=True, help="Source PDF for page-count checks."
)
QUALITY_OUTPUT_OPTION = typer.Option(
    None, dir_okay=False, help="Defaults to <parse-dir>/quality/report.json."
)

BACKUP_ARGUMENT = typer.Argument(..., dir_okay=False)
VERIFY_BACKUP_ARGUMENT = typer.Argument(..., exists=True, dir_okay=False)
SCALE_OUTPUT_ARGUMENT = typer.Argument(Path("data/benchmarks/scale/latest.json"), dir_okay=False)


@app.command("check-config")
def check_config() -> None:
    """Show which integrations are configured without revealing credentials."""
    settings = get_settings()
    typer.echo(
        json.dumps(
            {
                "mineru": settings.mineru_api_token is not None,
                "paddleocr": settings.paddleocr_access_token is not None,
                "siliconflow": settings.siliconflow_api_key is not None,
                "auth_mode": settings.auth_mode,
                "langfuse_enabled": settings.langfuse_enabled,
                "langfuse_configured": bool(
                    settings.langfuse_public_key and settings.langfuse_secret_key
                ),
                "langfuse_capture_content": settings.langfuse_capture_content,
                "parse_quality_mode": settings.parse_quality_mode,
                "parse_quality_accept_score": settings.parse_quality_accept_score,
                "parse_quality_review_score": settings.parse_quality_review_score,
                "parse_fallback_mode": settings.parse_fallback_mode,
                "parse_route_min_score_improvement": (settings.parse_route_min_score_improvement),
                "data_dir": str(settings.data_dir.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("langfuse-smoke")
def langfuse_smoke() -> None:
    """Check Langfuse auth and export a synthetic trace without calling an LLM."""
    telemetry = Telemetry(get_settings())
    if telemetry.client is None:
        raise typer.BadParameter("Enable SCHOLARRAG_LANGFUSE_ENABLED and configure project keys")

    async def run() -> None:
        try:
            try:
                authenticated = await asyncio.to_thread(telemetry.client.auth_check)
            except Exception as exc:
                raise typer.BadParameter(
                    "Langfuse connection/auth failed; check URL and keys"
                ) from exc
            if not authenticated:
                raise typer.BadParameter(
                    "Langfuse authentication failed; check URL and project keys"
                )
            trace_id = uuid.uuid4().hex
            with telemetry.trace(
                "scholarrag.smoke",
                trace_id=trace_id,
                session_id="scholarrag-learning",
                metadata={"synthetic": True},
                input="Synthetic smoke test; no paper or LLM request",
            ) as observation:
                from scholarrag.observability import observe

                with observe("example.retrieval", as_type="retriever"):
                    pass
                with observe("example.generation", as_type="generation", model="synthetic"):
                    pass
                observation.update(output="Smoke test finished")
            await asyncio.to_thread(telemetry.client.flush)
            typer.echo(json.dumps({"authenticated": True, "trace_id": trace_id, "synthetic": True}))
        finally:
            await asyncio.to_thread(telemetry.shutdown)

    asyncio.run(run())


@app.command("backup")
def backup(target: Path = BACKUP_ARGUMENT) -> None:
    """Create a checksummed archive of local source, artifacts, indexes and run history."""
    result = BackupService(get_settings()).create(target)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("verify-backup")
def verify_backup(archive: Path = VERIFY_BACKUP_ARGUMENT) -> None:
    """Verify every archived file against the immutable backup manifest."""
    result = BackupService.verify(archive)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["valid"]:
        raise typer.Exit(code=1)


@app.command("benchmark-scale")
def benchmark_scale(
    output: Path = SCALE_OUTPUT_ARGUMENT,
    paper_count: int = typer.Option(3000, min=100),
    chunks_per_paper: int = typer.Option(3, min=1, max=20),
) -> None:
    """Run an isolated synthetic SQLite/Qdrant capacity and backup-restore benchmark."""
    result = ScaleBenchmarkRunner(
        paper_count=paper_count,
        chunks_per_paper=chunks_per_paper,
    ).run(output)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("audit-parse-quality")
def audit_parse_quality(
    canonical: Path = CANONICAL_ARGUMENT,
    source_pdf: Path | None = SOURCE_PDF_OPTION,
    output: Path | None = QUALITY_OUTPUT_OPTION,
) -> None:
    """Evaluate a frozen canonical parse without calling a paid provider API."""
    settings = get_settings()
    document = CanonicalDocument.model_validate_json(canonical.read_text(encoding="utf-8"))
    expected_page_count = (
        IngestionService._pdf_page_count(source_pdf.resolve()) if source_pdf else None
    )
    report = ParseQualityGate(
        accept_score=settings.parse_quality_accept_score,
        review_score=settings.parse_quality_review_score,
    ).evaluate(document, expected_page_count=expected_page_count)
    target = output or canonical.parent.parent / "quality" / "report.json"
    ArtifactStore.write_json(target, report.model_dump(mode="json"))
    typer.echo(
        json.dumps(
            {
                "parse_id": report.parse_id,
                "score": report.score,
                "decision": report.decision,
                "recommended_action": report.recommended_action,
                "critical_failures": report.critical_failures,
                "report_path": str(target.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("repair-heading-hierarchy")
def repair_heading_hierarchy(
    document_id: str = typer.Argument(..., help="Document whose active parse should be repaired."),
    source_parse_id: str | None = typer.Option(
        None, help="Defaults to the document's active parse."
    ),
    activate: bool = typer.Option(
        False, help="Embed, index and activate the derived parse after preparing it."
    ),
) -> None:
    """Create an auditable parse from existing raw artifacts without paying to parse again."""
    settings = get_settings()
    result = HeadingRepairService(settings).prepare(document_id, source_parse_id)
    if activate:
        activation = asyncio.run(
            ParseReviewService(settings).activate(
                document_id,
                str(result["parse_id"]),
                reason="Activate deterministic heading-hierarchy repair",
            )
        )
        result["activation"] = activation.model_dump(mode="json")
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@app.command("smoke-siliconflow")
def smoke_siliconflow() -> None:
    async def run() -> None:
        client = SiliconFlowClient(get_settings())
        try:
            response = await client.embeddings(["ScholarRAG API connectivity test."])
            vector = response.data["data"][0]["embedding"]
            typer.echo(
                json.dumps(
                    {
                        "ok": True,
                        "model": response.data.get("model"),
                        "dimension": len(vector),
                        "trace_id": response.trace_id,
                    },
                    indent=2,
                )
            )
        finally:
            await client.close()

    asyncio.run(run())


@app.command("smoke-paddle")
def smoke_paddle(pdf: Path = PDF_ARGUMENT) -> None:
    """Parse only the first page through PaddleOCR's hosted official API."""

    async def run() -> None:
        client = PaddleOCRAPIClient(get_settings())
        result = await client.parse_document(pdf.resolve(), page_ranges="1")
        payload = client.to_dict(result)
        typer.echo(
            json.dumps(
                {
                    "ok": True,
                    "job_id": payload.get("job_id") or payload.get("jobId"),
                    "keys": sorted(payload),
                    "page_count": len(payload.get("pages", [])),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    asyncio.run(run())


@app.command("benchmark-parsers")
def benchmark_parsers(
    pdf: Path = PDF_ARGUMENT,
    page_ranges: str | None = PAGE_RANGES_OPTION,
    mineru_parse_dir: Path | None = MINERU_PARSE_DIR_OPTION,
) -> None:
    """Compare frozen MinerU, PaddleOCR-VL and PP-StructureV3 artifacts."""

    async def run() -> None:
        result_dir = await ParserBenchmarkRunner(get_settings()).run(
            pdf,
            page_ranges=page_ranges,
            mineru_parse_dir=mineru_parse_dir,
        )
        typer.echo(json.dumps({"ok": True, "result_dir": str(result_dir)}, indent=2))

    asyncio.run(run())


@app.command("resume-parser-benchmark")
def resume_parser_benchmark(
    run_dir: Path = BENCHMARK_RUN_ARGUMENT,
) -> None:
    """Resume from frozen raw responses without repeating completed paid parses."""

    async def run() -> None:
        result_dir = await ParserBenchmarkRunner(get_settings()).resume(run_dir)
        typer.echo(json.dumps({"ok": True, "result_dir": str(result_dir)}, indent=2))

    asyncio.run(run())


@app.command("normalize-paddle-benchmark")
def normalize_paddle_benchmark(
    run_dir: Path = BENCHMARK_RUN_ARGUMENT,
) -> None:
    """Normalize frozen Paddle responses without changing the active document parse."""
    outputs = ParserBenchmarkRunner(get_settings()).normalize_paddle_variants(run_dir)
    typer.echo(json.dumps(outputs, ensure_ascii=False, indent=2))


@app.command("prepare-parser-gold")
def prepare_parser_gold(
    manifest: Path = PARSER_GOLD_MANIFEST_ARGUMENT,
    paper_dir: Path = PAPER_DIR_OPTION,
) -> None:
    """Verify sources and render proposed pages without overwriting annotations."""
    output = ParserGoldPreparer(get_settings()).prepare(manifest, paper_dir=paper_dir)
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "sample_count": output["sample_count"],
                "prepared_manifest": output["prepared_manifest"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("benchmark-rag-variants")
def benchmark_rag_variants(
    run_dir: Path = BENCHMARK_RUN_ARGUMENT,
    dataset: Path = EVALUATION_DATASET_ARGUMENT,
) -> None:
    """Build isolated parser indexes and compare downstream RAG/Ragas results."""

    async def run() -> None:
        report = await ParserBenchmarkRunner(get_settings()).benchmark_rag_variants(
            run_dir, dataset
        )
        typer.echo(
            json.dumps(
                {
                    "ok": True,
                    "case_count": report["dataset_case_count"],
                    "variants": [item["variant"] for item in report["variants"]],
                    "report_path": report["report_path"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    asyncio.run(run())


@app.command("evaluate")
def evaluate(
    dataset: Path = EVALUATION_DATASET_ARGUMENT,
    allow_candidates: bool = typer.Option(
        False,
        help="Allow unreviewed candidate rows for smoke experiments; never for gold reporting.",
    ),
) -> None:
    """Run Ragas over a JSON or JSONL evaluation dataset and persist every run."""

    async def run() -> None:
        cases = load_cases(dataset)
        unreviewed = [case.case_id for case in cases if case.review_status != "reviewed"]
        if unreviewed and not allow_candidates:
            raise typer.BadParameter(
                f"Dataset contains {len(unreviewed)} unreviewed rows; "
                "review them or pass --allow-candidates for a non-gold smoke run."
            )
        evaluator = RagasEvaluator(get_settings())
        outputs = []
        for case in cases:
            result = await evaluator.evaluate_rag(case)
            outputs.append(
                {
                    "run_id": result.run_id,
                    "question": result.case.question,
                    "metrics": {name: score.value for name, score in result.metrics.items()},
                    "artifact_path": result.artifact_path,
                }
            )
        typer.echo(json.dumps(outputs, ensure_ascii=False, indent=2))

    asyncio.run(run())


@app.command("audit-parser-gold")
def audit_parser_gold(
    manifest: Path = PARSER_GOLD_MANIFEST_ARGUMENT,
) -> None:
    """Validate parser annotations and require a distinct second reviewer for gold status."""
    report = ParserGoldPreparer(get_settings()).audit(manifest)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["gold_ready"]:
        raise typer.Exit(code=2)


@app.command("audit-human-gates")
def audit_human_gates() -> None:
    """Persist and validate Parser Gold, QA triage, and Judge-human calibration gates."""
    report = HumanReviewService(get_settings()).write_gate_artifact()
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["human_gate_ready"]:
        raise typer.Exit(code=2)


@app.command("import-easydataset")
def import_easydataset(
    source: Path = EVALUATION_DATASET_ARGUMENT,
    target: Path = EVALUATION_TARGET_ARGUMENT,
    document_id: list[str] | None = DOCUMENT_IDS_OPTION,
) -> None:
    """Convert an EasyDataset custom JSON/JSONL export into reviewable eval candidates."""
    report = EasyDatasetAdapter.import_file(
        source,
        target,
        default_document_ids=document_id,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("export-easydataset-contexts")
def export_easydataset_contexts(target: Path = EVALUATION_TARGET_ARGUMENT) -> None:
    """Export active child/atomic chunks for use in the external EasyDataset workbench."""
    report = EasyDatasetContextExporter.export(get_settings(), target)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("generate-eval-candidates")
def generate_eval_candidates(
    manifest: Path = PARSER_GOLD_MANIFEST_ARGUMENT,
    paper_dir: Path = PAPER_DIR_OPTION,
    target: Path = EVALUATION_TARGET_ARGUMENT,
    synthetic: bool = typer.Option(
        False,
        "--synthetic",
        help="Mark generated rows as synthetic rather than human-review candidates.",
    ),
) -> None:
    """Generate page-grounded QA candidates or explicitly non-gold synthetic rows."""

    async def run() -> None:
        report = await EvaluationCandidateGenerator(get_settings()).generate_from_parser_manifest(
            manifest,
            paper_dir=paper_dir,
            target=target,
            review_status="synthetic" if synthetic else "candidate",
        )
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))

    asyncio.run(run())


@app.command("link-eval-dataset")
def link_eval_dataset(dataset: Path = EVALUATION_DATASET_ARGUMENT) -> None:
    """Link grounded evidence quotes to active chunk/block IDs for retrieval metrics."""
    result = EvaluationDatasetLinker.link(get_settings(), dataset)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("ask")
def ask(
    question: str,
    document_id: list[str] | None = DOCUMENT_IDS_OPTION,
) -> None:
    """Ask the indexed paper corpus a grounded question."""

    async def run() -> None:
        settings = get_settings()
        telemetry = Telemetry(settings)
        try:
            trace_id = uuid.uuid4().hex
            with telemetry.trace(
                "scholarrag.ask", trace_id=trace_id, input={"question": question}
            ) as observation:
                answer = await AgenticRAGService(settings).answer(
                    question, document_ids=document_id, trace_id=trace_id
                )
                observation.update(output=answer.answer)
                typer.echo(answer.model_dump_json(indent=2))
        finally:
            await asyncio.to_thread(telemetry.shutdown)

    asyncio.run(run())


@app.command("index-visual")
def index_visual(document_id: str) -> None:
    """Build Qwen3-VL image embeddings for an already-ingested paper."""

    async def run() -> None:
        settings = get_settings()
        record = Catalog(settings).get_document(document_id)
        if not record:
            raise typer.BadParameter(f"Unknown document: {document_id}")
        artifact_dir = Path(record["artifact_dir"])
        document = CanonicalDocument.model_validate_json(
            (artifact_dir / "canonical" / "document.json").read_text(encoding="utf-8")
        )
        chunks = [
            Chunk.model_validate(item)
            for item in json.loads(
                (artifact_dir / "chunks" / "chunks.json").read_text(encoding="utf-8")
            )
        ]
        count = await VisualIndex(settings).index(document, chunks)
        manifest_path = artifact_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["visual_indexed_count"] = count
        manifest["visual_indexed_at"] = datetime.now(UTC).isoformat()
        ArtifactStore.write_json(manifest_path, manifest)
        typer.echo(json.dumps({"document_id": document_id, "visual_blocks": count}, indent=2))

    asyncio.run(run())


@app.command("reindex-vl-text")
def reindex_vl_text(
    document_id: list[str] | None = DOCUMENT_IDS_OPTION,
) -> None:
    """Rebuild active text-chunk vectors in the unified Qwen3-VL embedding space."""

    async def run() -> None:
        settings = get_settings()
        catalog = Catalog(settings)
        selected = set(document_id or [])
        records = [
            record
            for record in catalog.list_documents()
            if not selected or str(record["document_id"]) in selected
        ]
        unknown = selected - {str(record["document_id"]) for record in records}
        if unknown:
            raise typer.BadParameter(f"Unknown document(s): {', '.join(sorted(unknown))}")
        index = VectorIndex(settings)
        results: list[dict[str, object]] = []
        for record in records:
            artifact_dir = Path(str(record["artifact_dir"]))
            chunks_path = artifact_dir / "chunks" / "chunks.json"
            if not chunks_path.is_file():
                raise typer.BadParameter(f"Missing chunk artifact: {chunks_path}")
            chunks = [
                Chunk.model_validate(item)
                for item in json.loads(chunks_path.read_text(encoding="utf-8"))
            ]
            count = await index.index(chunks)
            results.append(
                {
                    "document_id": str(record["document_id"]),
                    "parse_id": str(record["parse_id"]),
                    "indexed_chunks": count,
                }
            )
        typer.echo(
            json.dumps(
                {
                    "model": settings.unified_embedding_model,
                    "dimensions": settings.embedding_dimensions,
                    "collection": index.collection,
                    "document_count": len(results),
                    "indexed_chunks": sum(int(item["indexed_chunks"]) for item in results),
                    "documents": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    asyncio.run(run())


@app.command("gc-inactive-vectors")
def gc_inactive_vectors(document_id: str | None = None) -> None:
    """Delete Qdrant points for inactive parses; canonical/raw artifacts remain immutable."""

    async def run() -> None:
        settings = get_settings()
        parse_ids = Catalog(settings).inactive_parse_ids(document_id)
        text_count = await VectorIndex(settings).delete_parse_ids(parse_ids)
        visual_count = await VisualIndex(settings).delete_parse_ids(parse_ids)
        typer.echo(
            json.dumps(
                {
                    "inactive_parse_ids": parse_ids,
                    "text_filters_deleted": text_count,
                    "visual_filters_deleted": visual_count,
                    "artifacts_deleted": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    asyncio.run(run())


@app.command("smoke-mineru")
def smoke_mineru(
    pdf: Path = PDF_ARGUMENT,
    wait: bool = WAIT_OPTION,
) -> None:
    async def run() -> None:
        client = MinerUClient(get_settings())
        try:
            job = await client.submit_local_file(
                pdf.resolve(), data_id=f"smoke-{uuid.uuid4().hex[:12]}"
            )
            typer.echo(json.dumps({"batch_id": job.batch_id, "trace_id": job.trace_id}, indent=2))
            if wait:
                result = await client.wait_for_result(job.batch_id)
                typer.echo(
                    json.dumps(client.summarize_response(result), ensure_ascii=False, indent=2)
                )
        finally:
            await client.close()

    asyncio.run(run())


@app.command("download-mineru")
def download_mineru(
    batch_id: str,
    destination: Path,
) -> None:
    """Download an already-completed MinerU batch for schema inspection."""

    async def run() -> None:
        client = MinerUClient(get_settings())
        try:
            result = await client.get_batch(batch_id)
            target = await client.download_and_extract(result, destination.resolve())
            typer.echo(str(target))
        finally:
            await client.close()

    asyncio.run(run())


@app.command("ingest")
def ingest(
    pdf: Path = PDF_ARGUMENT,
    workspace: str = typer.Option(..., help="Target workspace ID."),
    api_url: str = typer.Option("http://127.0.0.1:8000", help="ScholarRAG session-mode API."),
    idempotency_key: str | None = typer.Option(
        None, help="Reuse this key when retrying the same upload."
    ),
) -> None:
    """Submit to the unified workspace queue; never call a paid parser directly."""
    token = os.environ.get("SCHOLARRAG_SESSION_TOKEN", "").strip()
    if not token:
        raise typer.BadParameter("Set SCHOLARRAG_SESSION_TOKEN to an issued user session token")
    key = idempotency_key or uuid.uuid4().hex
    typer.echo(json.dumps({"idempotency_key": key}), err=True)
    try:
        with pdf.open("rb") as source, httpx.Client(timeout=300) as client:
            response = client.post(
                f"{api_url.rstrip('/')}/api/workspaces/{workspace}/uploads",
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
                files={"file": (pdf.name, source, "application/pdf")},
            )
    except (OSError, httpx.HTTPError) as exc:
        raise typer.BadParameter(
            f"Upload failed ({type(exc).__name__}); retry with the same idempotency key"
        ) from None
    if response.status_code != 202:
        raise typer.BadParameter(f"Upload rejected (HTTP {response.status_code})")
    typer.echo(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
