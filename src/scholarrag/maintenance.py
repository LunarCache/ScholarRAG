from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scholarrag.config import Settings


class BackupService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create(self, target: Path) -> dict[str, Any]:
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        roots = [
            ("data", self.settings.data_dir),
            ("evals", self.settings.evals_dir),
        ]
        files = [
            (prefix, root, path)
            for prefix, root in roots
            if root.is_dir()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
        manifest = {
            "schema_version": "1.0",
            "created_at": datetime.now(UTC).isoformat(),
            "data_dir": str(self.settings.data_dir.resolve()),
            "evals_dir": str(self.settings.evals_dir.resolve()),
            "files": [],
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for prefix, root, path in files:
                relative = path.relative_to(root)
                digest = self._sha256(path)
                archive_path = Path(prefix) / relative
                archive.write(path, archive_path)
                manifest["files"].append(
                    {
                        "path": str(archive_path),
                        "sha256": digest,
                        "bytes": path.stat().st_size,
                    }
                )
            archive.writestr(
                "backup-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
            )
        manifest["archive"] = str(target)
        manifest["archive_bytes"] = target.stat().st_size
        return manifest

    @classmethod
    def verify(cls, archive_path: Path) -> dict[str, Any]:
        failures: list[dict[str, str]] = []
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("backup-manifest.json"))
            for item in manifest["files"]:
                try:
                    payload = archive.read(item["path"])
                except KeyError:
                    failures.append({"path": item["path"], "reason": "missing"})
                    continue
                digest = hashlib.sha256(payload).hexdigest()
                if digest != item["sha256"]:
                    failures.append({"path": item["path"], "reason": "checksum_mismatch"})
        return {
            "valid": not failures,
            "file_count": len(manifest["files"]),
            "failures": failures,
            "created_at": manifest["created_at"],
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
