from __future__ import annotations

import io

import httpx
import pymupdf
import pytest
from fastapi import HTTPException, UploadFile
from test_workspace_identity import team as team  # noqa: PLC0414

from scholarrag.api.main import create_app
from scholarrag.api.uploads import save_pdf_upload
from scholarrag.config import Settings


def pdf_bytes(pages=1):
    with pymupdf.open() as pdf:
        for _ in range(pages):
            pdf.new_page()
        return pdf.tobytes()


@pytest.mark.parametrize(
    ("content", "kwargs", "status"),
    [
        (b"not a PDF", {}, 422),
        (b"%PDF-broken", {}, 422),
        (pdf_bytes(), {"upload_max_bytes": 20}, 413),
        (pdf_bytes(2), {"upload_max_pages": 1}, 413),
    ],
)
async def test_invalid_or_oversized_upload_is_cleaned(tmp_path, content, kwargs, status):
    settings = Settings(data_dir=tmp_path, **kwargs)
    file = UploadFile(file=io.BytesIO(content), filename="paper.pdf")
    with pytest.raises(HTTPException) as error:
        await save_pdf_upload(file, settings)
    assert error.value.status_code == status
    assert file.file.closed
    assert list((tmp_path / "uploads").glob("*")) == []


async def test_upload_validation_precedes_enqueue_and_duplicate_is_cleaned(team):
    settings = team.settings
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.headers.update(
            {
                "Authorization": f"Bearer {team.sessions['owner'][1]}",
                "Idempotency-Key": "upload-test",
            }
        )
        url = f"/api/workspaces/{team.workspace}/uploads"
        invalid = await c.post(url, files={"file": ("bad.pdf", b"invalid")})
        assert invalid.status_code == 422
        assert app.state.runtime.workspace_ingestions.list(team.owner) == []
        data = pdf_bytes()
        first = await c.post(url, files={"file": ("paper.pdf", data)})
        second = await c.post(url, files={"file": ("paper.pdf", data)})
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(list((settings.data_dir / "workspaces" / team.workspace / "uploads").glob("*"))) == 1
