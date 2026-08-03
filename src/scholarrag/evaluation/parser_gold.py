from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pymupdf
from jsonschema import Draft202012Validator, FormatChecker

from scholarrag.config import Settings
from scholarrag.storage.artifacts import ArtifactStore


class ParserGoldError(RuntimeError):
    pass


class ParserGoldPreparer:
    """Verify immutable PDF inputs, render proposed pages, and create safe templates."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = ArtifactStore(settings)

    def prepare(self, manifest_path: Path, *, paper_dir: Path) -> dict[str, Any]:
        manifest_path = manifest_path.resolve()
        paper_dir = paper_dir.resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        schema_path = manifest_path.parent / "schema.json"
        if not schema_path.exists():
            raise ParserGoldError(f"Missing annotation schema: {schema_path}")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        annotation_dir = manifest_path.parent / "annotations"
        page_dir = self.settings.data_dir / "evaluations" / "parser-gold" / "pages"
        annotation_dir.mkdir(parents=True, exist_ok=True)
        page_dir.mkdir(parents=True, exist_ok=True)

        prepared: list[dict[str, Any]] = []
        seen: set[str] = set()
        for paper in manifest.get("papers", []):
            source = paper_dir / paper["source_filename"]
            if not source.is_file():
                raise ParserGoldError(f"Missing source PDF: {source}")
            actual_hash = self.store.sha256(source)
            if actual_hash != paper["source_sha256"]:
                raise ParserGoldError(f"Source hash mismatch: {source}")
            with pymupdf.open(source) as pdf:
                if pdf.page_count != paper["page_count"]:
                    raise ParserGoldError(
                        f"Page count mismatch for {source.name}: "
                        f"expected {paper['page_count']}, got {pdf.page_count}"
                    )
                for sample in paper.get("samples", []):
                    sample_id = sample["sample_id"]
                    if sample_id in seen:
                        raise ParserGoldError(f"Duplicate sample ID: {sample_id}")
                    seen.add(sample_id)
                    page_number = int(sample["page_number"])
                    if page_number < 1 or page_number > pdf.page_count:
                        raise ParserGoldError(
                            f"Invalid page {page_number} for {source.name} ({pdf.page_count})"
                        )
                    image_path = page_dir / f"{sample_id}@2x.png"
                    if not image_path.exists():
                        page = pdf.load_page(page_number - 1)
                        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                        pixmap.save(image_path)
                    image_hash = self.store.sha256(image_path)
                    annotation_path = annotation_dir / f"{sample_id}.json"
                    if not annotation_path.exists():
                        self.store.write_json(
                            annotation_path,
                            {
                                "schema_version": "1.0",
                                "sample_id": sample_id,
                                "source_sha256": actual_hash,
                                "source_filename": source.name,
                                "page_number": page_number,
                                "page_image_sha256": image_hash,
                                "status": "proposed",
                                "tags": sample.get("tags", []),
                                "notes": "Awaiting blind human annotation and independent review.",
                                "elements": [],
                                "qa_cases": [],
                                "annotation": {},
                            },
                        )
                    errors = list(
                        validator.iter_errors(
                            json.loads(annotation_path.read_text(encoding="utf-8"))
                        )
                    )
                    if errors:
                        raise ParserGoldError(
                            f"Invalid annotation {annotation_path}: {errors[0].message}"
                        )
                    prepared.append(
                        {
                            "sample_id": sample_id,
                            "source_filename": source.name,
                            "source_sha256": actual_hash,
                            "page_number": page_number,
                            "tags": sample.get("tags", []),
                            "page_image": str(image_path.resolve()),
                            "page_image_sha256": image_hash,
                            "annotation": str(annotation_path.resolve()),
                        }
                    )

        declared_count = int(manifest.get("sample_count", -1))
        if declared_count != len(prepared):
            raise ParserGoldError(
                f"Manifest declares {declared_count} samples, prepared {len(prepared)}"
            )
        output = {
            "schema_version": "1.0",
            "source_manifest": str(manifest_path),
            "sample_count": len(prepared),
            "samples": prepared,
        }
        output_path = self.settings.data_dir / "evaluations" / "parser-gold" / "prepared.json"
        self.store.write_json(output_path, output)
        output["prepared_manifest"] = str(output_path.resolve())
        return output

    def audit(self, manifest_path: Path) -> dict[str, Any]:
        """Validate annotations and enforce independent-review semantics."""
        root = manifest_path.resolve().parent
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        schema = json.loads((root / "schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        expected = [
            {
                "sample_id": sample["sample_id"],
                "source_filename": paper["source_filename"],
                "source_sha256": paper["source_sha256"],
                "page_number": int(sample["page_number"]),
            }
            for paper in manifest.get("papers", [])
            for sample in paper.get("samples", [])
        ]
        counts = {"proposed": 0, "annotated": 0, "reviewed": 0, "invalid": 0}
        failures: list[dict[str, str]] = []
        for declared in expected:
            sample_id = declared["sample_id"]
            path = root / "annotations" / f"{sample_id}.json"
            if not path.is_file():
                counts["invalid"] += 1
                failures.append({"sample_id": sample_id, "reason": "missing_annotation"})
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            errors = list(validator.iter_errors(payload))
            if errors:
                counts["invalid"] += 1
                failures.append({"sample_id": sample_id, "reason": f"schema:{errors[0].message}"})
                continue
            for field in ("sample_id", "source_filename", "source_sha256", "page_number"):
                if payload.get(field) != declared[field]:
                    counts["invalid"] += 1
                    failures.append(
                        {"sample_id": sample_id, "reason": f"manifest_mismatch:{field}"}
                    )
                    break
            else:
                image = (
                    self.settings.data_dir
                    / "evaluations"
                    / "parser-gold"
                    / "pages"
                    / f"{sample_id}@2x.png"
                )
                if not image.is_file() or payload.get("page_image_sha256") != self.store.sha256(
                    image
                ):
                    counts["invalid"] += 1
                    failures.append({"sample_id": sample_id, "reason": "page_image_hash_mismatch"})
                    continue
                content_reason = self._content_failure(payload)
                if content_reason:
                    counts["invalid"] += 1
                    failures.append({"sample_id": sample_id, "reason": content_reason})
                    continue
                status = str(payload["status"])
                annotation = payload.get("annotation") or {}
                reason = self._review_failure(status, annotation)
                if reason:
                    counts["invalid"] += 1
                    failures.append({"sample_id": sample_id, "reason": reason})
                    continue
                counts[status] += 1
                continue
            continue
        return {
            "schema_version": "1.0",
            "sample_count": len(expected),
            "counts": counts,
            "gold_ready": counts["reviewed"] == len(expected) and not failures,
            "failures": failures,
        }

    @staticmethod
    def _review_failure(status: str, annotation: dict[str, Any]) -> str | None:
        annotator = str(annotation.get("annotator") or "").strip()
        reviewer = str(annotation.get("reviewer") or "").strip()
        if status in {"annotated", "reviewed"} and (
            not annotator or not annotation.get("annotated_at")
        ):
            return "annotated_status_requires_annotator_and_timestamp"
        if status == "reviewed":
            if not reviewer or not annotation.get("reviewed_at"):
                return "reviewed_status_requires_reviewer_and_timestamp"
            if reviewer.casefold() == annotator.casefold():
                return "reviewer_must_differ_from_annotator"
        return None

    @staticmethod
    def _content_failure(payload: dict[str, Any]) -> str | None:
        status = str(payload.get("status"))
        elements = payload.get("elements") or []
        if status in {"annotated", "reviewed"} and not elements:
            return "annotated_status_requires_elements"
        element_ids = [str(element.get("element_id") or "") for element in elements]
        if len(element_ids) != len(set(element_ids)):
            return "element_ids_must_be_unique"
        known = set(element_ids)
        reading_orders = [
            element.get("reading_order")
            for element in elements
            if element.get("reading_order") is not None
        ]
        if len(reading_orders) != len(set(reading_orders)):
            return "reading_order_must_be_unique"
        for element in elements:
            bbox = element.get("bbox") or {}
            if bbox and (
                bbox.get("x0", 0) >= bbox.get("x1", 0) or bbox.get("y0", 0) >= bbox.get("y1", 0)
            ):
                return "bbox_must_have_positive_area"
            for relation in ("caption_element_id", "parent_element_id"):
                target = element.get(relation)
                if target and target not in known:
                    return f"unknown_{relation}"
        for case in payload.get("qa_cases") or []:
            evidence = case.get("evidence_element_ids") or []
            if any(element_id not in known for element_id in evidence):
                return "qa_case_references_unknown_element"
            if case.get("answerable") and (not case.get("answer") or not evidence):
                return "answerable_qa_requires_answer_and_evidence"
        return None
