from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from scholarrag.evaluation.service import EvaluationCase


class EasyDatasetAdapter:
    """Boundary adapter for EasyDataset custom JSON/JSONL exports.

    EasyDataset stays an external AGPL workbench. Imported rows remain candidates until
    a human explicitly changes ``review_status`` to ``reviewed``.
    """

    QUESTION_FIELDS = ("question", "instruction", "query", "input")
    ANSWER_FIELDS = ("reference", "answer", "output", "response")

    @classmethod
    def import_file(
        cls,
        source: Path,
        target: Path,
        *,
        default_document_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        rows = cls._read_rows(source)
        cases: list[EvaluationCase] = []
        rejected: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            question = cls._first_text(row, cls.QUESTION_FIELDS)
            reference = cls._first_text(row, cls.ANSWER_FIELDS)
            if not question or not reference:
                rejected.append({"row": index, "reason": "missing question or reference"})
                continue
            document_ids = cls._string_list(row.get("document_ids")) or (default_document_ids or [])
            tags = cls._string_list(row.get("tags") or row.get("labels") or row.get("domain"))
            cases.append(
                EvaluationCase(
                    case_id=str(row.get("case_id") or f"easy_{uuid.uuid4().hex}"),
                    question=question,
                    document_ids=document_ids or None,
                    reference=reference,
                    tags=tags,
                    source_chunk_ids=cls._string_list(row.get("source_chunk_ids")),
                    source_block_ids=cls._string_list(row.get("source_block_ids")),
                    review_status="candidate",
                    generator={
                        "tool": "EasyDataset",
                        "source_file": str(source.resolve()),
                        "source_row": index,
                    },
                )
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "".join(case.model_dump_json() + "\n" for case in cases), encoding="utf-8"
        )
        report = {
            "schema_version": "1.0",
            "source": str(source.resolve()),
            "target": str(target.resolve()),
            "imported": len(cases),
            "rejected": rejected,
            "review_status": "candidate",
            "note": "Human review is required before evaluation gold usage.",
        }
        target.with_suffix(target.suffix + ".import.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report

    @staticmethod
    def _read_rows(path: Path) -> list[dict[str, Any]]:
        if path.suffix.lower() == ".jsonl":
            payload = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("datasets") or [payload]
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("EasyDataset export must contain JSON objects")
        return payload

    @staticmethod
    def _first_text(row: dict[str, Any], fields: tuple[str, ...]) -> str | None:
        for field in fields:
            value = row.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []
