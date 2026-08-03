from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from scholarrag.config import Settings


class ArtifactStore:
    """Content-addressed source files plus versioned, append-only parse artifacts."""

    def __init__(self, settings: Settings) -> None:
        self.root = settings.artifacts_dir

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def document_dir(self, document_id: str) -> Path:
        return self.root / document_id

    def parse_dir(self, document_id: str, parse_id: str) -> Path:
        path = self.document_dir(document_id) / "parses" / parse_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def preserve_source(self, source: Path, document_id: str) -> tuple[Path, str]:
        source_hash = self.sha256(source)
        target_dir = self.document_dir(document_id) / "source"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{source_hash}{source.suffix.lower()}"
        if not target.exists():
            shutil.copy2(source, target)
        return target, source_hash

    @staticmethod
    def write_json(path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path
