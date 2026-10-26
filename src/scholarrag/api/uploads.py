"""Bound and validate uploads before a paid parser job is submitted."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import anyio
import pymupdf
from fastapi import HTTPException, UploadFile

from scholarrag.config import Settings


def _validate_pdf(path: Path, max_pages: int) -> None:
    try:
        with pymupdf.open(path) as pdf:
            if not pdf.is_pdf or pdf.needs_pass or pdf.page_count < 1:
                raise HTTPException(status_code=422, detail="PDF must be readable and unencrypted")
            if pdf.page_count > max_pages:
                raise HTTPException(status_code=413, detail="PDF page limit exceeded")
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid PDF") from exc


async def save_pdf_upload(
    file: UploadFile, settings: Settings, *, upload_dir: Path | None = None
) -> tuple[str, Path]:
    target: Path | None = None
    try:
        filename = re.sub(
            r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", Path(file.filename or "paper.pdf").name
        )[:180]
        if Path(filename).suffix.lower() != ".pdf":
            raise HTTPException(status_code=415, detail="Only PDF uploads are supported")
        upload_dir = upload_dir or settings.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / f"{uuid.uuid4().hex}-{filename}"
        total = 0
        header = b""
        async with await anyio.open_file(target, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > settings.upload_max_bytes:
                    raise HTTPException(status_code=413, detail="PDF byte limit exceeded")
                if len(header) < 1024:
                    header += chunk[: 1024 - len(header)]
                await output.write(chunk)
        if b"%PDF-" not in header:
            raise HTTPException(status_code=422, detail="Invalid PDF header")
        # Await validation to completion before cleanup, even if the request is cancelled.
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(_validate_pdf, target, settings.upload_max_pages)
        return filename, target
    except BaseException:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
