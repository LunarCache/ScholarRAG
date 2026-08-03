from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker

from scholarrag.config import Settings
from scholarrag.evaluation.parser_gold import ParserGoldPreparer
from scholarrag.storage.catalog import Catalog

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_GLOBAL_REVIEW_LOCK = threading.RLock()


class HumanReviewError(RuntimeError):
    pass


class HumanReviewService:
    """File-backed, auditable human review workflow.

    Human identity is recorded, never inferred. The service validates independent-review
    semantics but intentionally cannot assert that a typed name represents a real person.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.parser_root = settings.evals_dir / "parser-gold"
        self.qa_path = settings.evals_dir / "initial-42-candidates.jsonl"
        self.review_root = settings.data_dir / "evaluations" / "human-review"
        self.audit_path = self.review_root / "audit.jsonl"
        self.ratings_path = self.review_root / "judge-ratings.jsonl"
        self._lock = _GLOBAL_REVIEW_LOCK

    def summary(self) -> dict[str, Any]:
        parser = self.parser_audit()
        qa = self.qa_audit()
        judge = self.judge_calibration()
        return {
            "schema_version": "1.0",
            "parser_gold": parser,
            "qa_review": qa,
            "judge_calibration": judge,
            "human_gate_ready": bool(
                parser["gold_ready"] and qa["gold_ready"] and judge["gold_ready"]
            ),
            "identity_notice": (
                "Actor names are audit claims. ScholarRAG cannot verify that they identify "
                "distinct real people; organizational sign-off remains required."
            ),
        }

    def audit_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        if not self.audit_path.is_file():
            return []
        return self._read_jsonl(self.audit_path)[-max(1, min(limit, 5000)) :]

    def write_gate_artifact(self) -> dict[str, Any]:
        summary = self.summary()
        sources = {
            "parser_manifest": self.parser_root / "manifest.json",
            "qa_dataset": self.qa_path,
            "judge_ratings": self.ratings_path,
            "audit_log": self.audit_path,
        }
        payload = {
            **summary,
            "created_at": self._now(),
            "source_sha256": {
                name: self._file_sha256(path) if path.is_file() else None
                for name, path in sources.items()
            },
        }
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.review_root / f"gate_{stamp}.json"
        self._atomic_json(target, payload)
        payload["artifact_path"] = str(target.resolve())
        return payload

    def parser_audit(self) -> dict[str, Any]:
        manifest = self.parser_root / "manifest.json"
        if not manifest.is_file():
            return {
                "sample_count": 0,
                "counts": {"proposed": 0, "annotated": 0, "reviewed": 0, "invalid": 0},
                "gold_ready": False,
                "failures": [{"sample_id": "*", "reason": "manifest_missing"}],
            }
        return ParserGoldPreparer(self.settings).audit(manifest)

    def list_parser_samples(self) -> list[dict[str, Any]]:
        manifest_path = self.parser_root / "manifest.json"
        if not manifest_path.is_file():
            return []
        manifest = self._read_json(manifest_path)
        samples: list[dict[str, Any]] = []
        for paper in manifest.get("papers", []):
            for declared in paper.get("samples", []):
                sample_id = str(declared["sample_id"])
                path = self.parser_root / "annotations" / f"{sample_id}.json"
                if not path.is_file():
                    continue
                payload = self._read_json(path)
                samples.append(
                    {
                        **payload,
                        "tags": payload.get("tags") or declared.get("tags", []),
                        "image_url": f"/api/reviews/parser-gold/{sample_id}/image",
                    }
                )
        return samples

    def parser_image(self, sample_id: str) -> Path:
        self._validate_id(sample_id)
        annotation = self._parser_annotation(sample_id)
        image = (
            self.settings.data_dir / "evaluations" / "parser-gold" / "pages" / f"{sample_id}@2x.png"
        )
        if not image.is_file():
            raise HumanReviewError("Rendered parser-gold page is missing")
        expected = annotation.get("page_image_sha256")
        if expected and self._file_sha256(image) != expected:
            raise HumanReviewError("Rendered page hash no longer matches the annotation")
        return image

    def annotate_parser(
        self,
        sample_id: str,
        *,
        actor: str,
        elements: list[dict[str, Any]],
        qa_cases: list[dict[str, Any]],
        notes: str,
    ) -> dict[str, Any]:
        actor = self._actor(actor)
        if not elements:
            raise HumanReviewError("Annotated parser-gold pages require at least one element")
        with self._lock:
            before = self._parser_annotation(sample_id)
            if before.get("status") == "reviewed":
                raise HumanReviewError("Reviewed samples must be reopened before editing")
            existing_actor = str((before.get("annotation") or {}).get("annotator") or "")
            if existing_actor and existing_actor.casefold() != actor.casefold():
                raise HumanReviewError("Only the recorded annotator may revise this sample")
            after = dict(before)
            after.update(
                {
                    "status": "annotated",
                    "elements": elements,
                    "qa_cases": qa_cases,
                    "notes": notes,
                    "annotation": {
                        "annotator": actor,
                        "annotated_at": self._now(),
                    },
                }
            )
            self._validate_parser(after)
            self._write_parser(sample_id, before, after, actor=actor, action="annotate")
            return after

    def review_parser(self, sample_id: str, *, actor: str) -> dict[str, Any]:
        actor = self._actor(actor)
        with self._lock:
            before = self._parser_annotation(sample_id)
            if before.get("status") != "annotated":
                raise HumanReviewError("Only annotated samples can receive independent review")
            annotation = dict(before.get("annotation") or {})
            annotator = str(annotation.get("annotator") or "")
            if actor.casefold() == annotator.casefold():
                raise HumanReviewError("Reviewer must differ from annotator")
            annotation.update({"reviewer": actor, "reviewed_at": self._now()})
            after = {**before, "status": "reviewed", "annotation": annotation}
            self._validate_parser(after)
            self._write_parser(sample_id, before, after, actor=actor, action="review")
            return after

    def reopen_parser(self, sample_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        actor = self._actor(actor)
        reason = reason.strip()
        if len(reason) < 3:
            raise HumanReviewError("Reopen reason must contain at least 3 characters")
        with self._lock:
            before = self._parser_annotation(sample_id)
            if before.get("status") != "reviewed":
                raise HumanReviewError("Only reviewed samples can be reopened")
            annotation = before.get("annotation") or {}
            after = {
                **before,
                "status": "annotated",
                "annotation": {
                    "annotator": annotation.get("annotator"),
                    "annotated_at": annotation.get("annotated_at"),
                },
                "notes": f"{before.get('notes', '')}\nREOPEN: {reason}".strip(),
            }
            self._validate_parser(after)
            self._write_parser(
                sample_id, before, after, actor=actor, action="reopen", reason=reason
            )
            return after

    def qa_cases(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self.qa_path) if self.qa_path.is_file() else []

    def qa_cases_with_evidence(self) -> list[dict[str, Any]]:
        cases = self.qa_cases()
        catalog = Catalog(self.settings)
        for case in cases:
            chunk_ids = case.get("source_chunk_ids") or []
            chunks = catalog.chunks_by_ids(chunk_ids)
            case["evidence_preview"] = [
                {
                    "chunk_id": chunk_id,
                    "document_id": chunk.document_id,
                    "page_numbers": chunk.page_numbers,
                    "block_ids": chunk.block_ids,
                    "text": chunk.text[:4000],
                }
                for chunk_id in chunk_ids
                if (chunk := chunks.get(chunk_id)) is not None
            ]
        return cases

    def qa_audit(self) -> dict[str, Any]:
        cases = self.qa_cases()
        catalog = Catalog(self.settings)
        counts = {
            "candidate": 0,
            "synthetic": 0,
            "reviewed": 0,
            "rejected": 0,
            "invalid": 0,
        }
        failures: list[dict[str, str]] = []
        for case in cases:
            status = str(case.get("review_status") or "candidate")
            if status not in {"candidate", "synthetic", "reviewed", "rejected"}:
                counts["invalid"] += 1
                failures.append({"case_id": str(case.get("case_id")), "reason": "invalid_status"})
                continue
            counts[status] += 1
            if status == "reviewed":
                review = (case.get("generator") or {}).get("human_review") or {}
                if not review.get("reviewer") or not review.get("reviewed_at"):
                    counts["invalid"] += 1
                    failures.append(
                        {"case_id": str(case.get("case_id")), "reason": "missing_reviewer"}
                    )
                source_chunk_ids = case.get("source_chunk_ids") or []
                chunks = catalog.chunks_by_ids(source_chunk_ids)
                if not source_chunk_ids or len(chunks) != len(set(source_chunk_ids)):
                    counts["invalid"] += 1
                    failures.append(
                        {
                            "case_id": str(case.get("case_id")),
                            "reason": "missing_or_inactive_source_chunk",
                        }
                    )
        gold_ready = bool(
            cases
            and counts["candidate"] == 0
            and counts["synthetic"] == 0
            and counts["invalid"] == 0
            and counts["reviewed"] >= 30
            and counts["reviewed"] + counts["rejected"] == len(cases)
        )
        return {
            "case_count": len(cases),
            "counts": counts,
            "minimum_reviewed": 30,
            "all_candidates_triaged": bool(cases)
            and counts["candidate"] == 0
            and counts["synthetic"] == 0,
            "gold_ready": gold_ready,
            "failures": failures,
        }

    def review_qa(
        self,
        case_id: str,
        *,
        actor: str,
        decision: Literal["reviewed", "rejected", "candidate"],
        question: str,
        reference: str,
        tags: list[str],
        source_chunk_ids: list[str],
        source_block_ids: list[str],
        note: str,
    ) -> dict[str, Any]:
        self._validate_id(case_id)
        actor = self._actor(actor)
        question, reference = question.strip(), reference.strip()
        if not question or not reference:
            raise HumanReviewError("Question and reference answer are required")
        if decision == "reviewed":
            if not source_chunk_ids:
                raise HumanReviewError("Reviewed QA must link at least one active source chunk")
            chunks = Catalog(self.settings).chunks_by_ids(source_chunk_ids)
            missing = sorted(set(source_chunk_ids) - set(chunks))
            if missing:
                raise HumanReviewError(f"Unknown or inactive source chunks: {', '.join(missing)}")
            linked_blocks = {block_id for chunk in chunks.values() for block_id in chunk.block_ids}
            unknown_blocks = sorted(set(source_block_ids) - linked_blocks)
            if unknown_blocks:
                raise HumanReviewError(
                    f"Source blocks are not contained by linked chunks: {', '.join(unknown_blocks)}"
                )
        with self._lock:
            cases = self.qa_cases()
            index = next((i for i, item in enumerate(cases) if item.get("case_id") == case_id), -1)
            if index < 0:
                raise HumanReviewError("QA case not found")
            before = cases[index]
            generator = dict(before.get("generator") or {})
            generator["human_review"] = {
                "reviewer": actor,
                "reviewed_at": self._now(),
                "decision": decision,
                "note": note.strip(),
            }
            after = {
                **before,
                "question": question,
                "reference": reference,
                "tags": self._unique(tags),
                "source_chunk_ids": self._unique(source_chunk_ids),
                "source_block_ids": self._unique(source_block_ids),
                "review_status": decision,
                "generator": generator,
            }
            cases[index] = after
            self._atomic_jsonl(self.qa_path, cases)
            self._append_audit(
                kind="qa", item_id=case_id, actor=actor, action=decision, before=before, after=after
            )
            return after

    def evaluation_runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for path in sorted((self.settings.data_dir / "evaluations").glob("eval_*.json")):
            payload = self._read_json(path)
            runs.append(
                {
                    "run_id": payload.get("run_id"),
                    "created_at": payload.get("created_at"),
                    "question": (payload.get("case") or {}).get("question"),
                    "answer": payload.get("answer"),
                    "metrics": {
                        name: score.get("value") if isinstance(score, dict) else score
                        for name, score in (payload.get("metrics") or {}).items()
                    },
                }
            )
        return runs

    def submit_rating(
        self,
        run_id: str,
        *,
        rater: str,
        faithfulness: float,
        answer_relevancy: float,
        citation_correctness: float,
        notes: str,
    ) -> dict[str, Any]:
        self._validate_id(run_id)
        rater = self._actor(rater)
        scores = {
            "faithfulness": self._score(faithfulness),
            "answer_relevancy": self._score(answer_relevancy),
            "citation_correctness": self._score(citation_correctness),
        }
        if run_id not in {str(item["run_id"]) for item in self.evaluation_runs()}:
            raise HumanReviewError("Evaluation run not found")
        rating = {
            "schema_version": "1.0",
            "rating_id": f"rating_{hashlib.sha256(f'{run_id}:{rater}:{self._now()}'.encode()).hexdigest()[:24]}",
            "run_id": run_id,
            "rater": rater,
            "rated_at": self._now(),
            "scores": scores,
            "notes": notes.strip(),
        }
        with self._lock:
            self.review_root.mkdir(parents=True, exist_ok=True)
            with self.ratings_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(rating, ensure_ascii=False) + "\n")
            self._append_audit(
                kind="judge_rating",
                item_id=run_id,
                actor=rater,
                action="rate",
                before={},
                after=rating,
            )
        return rating

    def judge_calibration(self) -> dict[str, Any]:
        ratings = self._read_jsonl(self.ratings_path) if self.ratings_path.is_file() else []
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for rating in ratings:
            latest[(str(rating.get("run_id")), str(rating.get("rater")).casefold())] = rating
        by_run: dict[str, list[dict[str, Any]]] = {}
        for rating in latest.values():
            by_run.setdefault(str(rating["run_id"]), []).append(rating)
        runs = {str(item["run_id"]): item for item in self.evaluation_runs()}
        qualified = {run_id: items for run_id, items in by_run.items() if len(items) >= 2}
        correlations: dict[str, dict[str, float | int | None]] = {}
        mapping = {
            "faithfulness": "faithfulness",
            "answer_relevancy": "answer_relevancy",
            "citation_correctness": "citation_validity",
        }
        for human_name, judge_name in mapping.items():
            judge_values: list[float] = []
            human_values: list[float] = []
            for run_id, items in qualified.items():
                judge = (runs.get(run_id) or {}).get("metrics", {}).get(judge_name)
                if not isinstance(judge, (int, float)):
                    continue
                judge_values.append(float(judge))
                human_values.append(
                    sum(float(item["scores"][human_name]) for item in items) / len(items)
                )
            correlations[human_name] = {
                "samples": len(judge_values),
                "pearson": self._pearson(judge_values, human_values),
                "spearman": self._pearson(self._ranks(judge_values), self._ranks(human_values)),
                "mean_bias": (
                    sum(judge - human for judge, human in zip(judge_values, human_values))
                    / len(judge_values)
                    if judge_values
                    else None
                ),
            }
        rater_names = sorted({str(item["rater"]) for item in latest.values()})
        return {
            "rating_events": len(ratings),
            "latest_ratings": len(latest),
            "distinct_raters": rater_names,
            "double_rated_runs": len(qualified),
            "minimum_double_rated_runs": 30,
            "correlations": correlations,
            "gold_ready": len(qualified) >= 30 and len(rater_names) >= 2,
        }

    def _parser_annotation(self, sample_id: str) -> dict[str, Any]:
        self._validate_id(sample_id)
        path = self.parser_root / "annotations" / f"{sample_id}.json"
        if not path.is_file():
            raise HumanReviewError("Parser-gold sample not found")
        return self._read_json(path)

    def _write_parser(
        self,
        sample_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        actor: str,
        action: str,
        reason: str | None = None,
    ) -> None:
        path = self.parser_root / "annotations" / f"{sample_id}.json"
        self._atomic_json(path, after)
        self._append_audit(
            kind="parser_gold",
            item_id=sample_id,
            actor=actor,
            action=action,
            before=before,
            after=after,
            reason=reason,
        )

    def _validate_parser(self, payload: dict[str, Any]) -> None:
        schema_path = self.parser_root / "schema.json"
        if not schema_path.is_file():
            raise HumanReviewError("Parser-gold schema is missing")
        schema = self._read_json(schema_path)
        errors = list(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload)
        )
        if errors:
            raise HumanReviewError(f"Annotation schema error: {errors[0].message}")
        content_reason = ParserGoldPreparer._content_failure(payload)
        if content_reason:
            raise HumanReviewError(content_reason)
        reason = ParserGoldPreparer._review_failure(
            str(payload.get("status")), payload.get("annotation") or {}
        )
        if reason:
            raise HumanReviewError(reason)

    def _append_audit(
        self,
        *,
        kind: str,
        item_id: str,
        actor: str,
        action: str,
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        event = {
            "schema_version": "1.0",
            "event_id": f"audit_{hashlib.sha256(f'{kind}:{item_id}:{actor}:{self._now()}'.encode()).hexdigest()[:24]}",
            "created_at": self._now(),
            "kind": kind,
            "item_id": item_id,
            "actor": actor,
            "action": action,
            "reason": reason,
            "before_sha256": self._payload_hash(before),
            "after_sha256": self._payload_hash(after),
        }
        self.review_root.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise HumanReviewError(f"Expected JSON object: {path}")
        return payload

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not all(isinstance(row, dict) for row in rows):
            raise HumanReviewError(f"Expected JSONL objects: {path}")
        return rows

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    @staticmethod
    def _atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _validate_id(value: str) -> None:
        if not _SAFE_ID.fullmatch(value):
            raise HumanReviewError("Invalid artifact identifier")

    @staticmethod
    def _actor(value: str) -> str:
        actor = value.strip()
        if len(actor) < 2 or len(actor) > 120:
            raise HumanReviewError("Actor name must contain 2-120 characters")
        return actor

    @staticmethod
    def _score(value: float) -> float:
        result = float(value)
        if not math.isfinite(result) or result < 0 or result > 1:
            raise HumanReviewError("Human scores must be finite values between 0 and 1")
        return result

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in values if item.strip()))

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _pearson(left: list[float], right: list[float]) -> float | None:
        if len(left) < 2 or len(left) != len(right):
            return None
        left_mean, right_mean = sum(left) / len(left), sum(right) / len(right)
        numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
        left_ss = sum((x - left_mean) ** 2 for x in left)
        right_ss = sum((y - right_mean) ** 2 for y in right)
        denominator = math.sqrt(left_ss * right_ss)
        return numerator / denominator if denominator else None

    @staticmethod
    def _ranks(values: list[float]) -> list[float]:
        ordered = sorted(enumerate(values), key=lambda pair: pair[1])
        ranks = [0.0] * len(values)
        cursor = 0
        while cursor < len(ordered):
            end = cursor + 1
            while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
                end += 1
            rank = (cursor + 1 + end) / 2
            for index in range(cursor, end):
                ranks[ordered[index][0]] = rank
            cursor = end
        return ranks
