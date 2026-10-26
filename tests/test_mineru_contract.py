from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
from pydantic import ValidationError

from scholarrag.config import Settings
from scholarrag.ingestion.mineru_normalizer import MinerUNormalizer
from scholarrag.providers.mineru import MinerUClient, MinerUError


def result_zip():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "nested/paper_content_list_v2.json",
            json.dumps(
                [
                    [
                        {
                            "type": "title",
                            "content": {
                                "title_content": [{"type": "text", "content": "Synthetic title"}]
                            },
                        },
                        {
                            "type": "paragraph",
                            "content": {
                                "paragraph_content": [
                                    {"type": "text", "content": "Synthetic evidence."}
                                ]
                            },
                        },
                        {
                            "type": "image",
                            "content": {
                                "image_source": {"path": "images/figure.png"},
                                "image_caption": "Figure",
                            },
                        },
                    ]
                ]
            ),
        )
        archive.writestr(
            "nested/layout.json",
            json.dumps({"pdf_info": [{"page_idx": 0, "page_size": [600, 800]}]}),
        )
        archive.writestr("nested/images/figure.png", b"synthetic image bytes")
    return stream.getvalue()


async def test_hosted_v4_submission_poll_download_and_normalization(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"synthetic PDF bytes")
    states = iter(["waiting-file", "pending", "running", "converting", "done"])
    seen = []

    async def handler(request):
        seen.append(request)
        if request.url.host == "mineru.net":
            assert request.headers["authorization"] == "Bearer test-not-a-real-key"
            if request.method == "POST":
                payload = json.loads(request.content)
                assert request.url.path == "/api/v4/file-urls/batch"
                assert payload["model_version"] == "vlm"
                assert payload["language"] == "ch"
                assert payload["files"] == [
                    {
                        "name": "paper.pdf",
                        "data_id": "doc-test",
                        "is_ocr": True,
                        "page_ranges": "1-2",
                    }
                ]
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "batch_id": "batch-test",
                            "file_urls": ["https://storage.example/upload"],
                        },
                    },
                )
            assert request.url.path == "/api/v4/extract-results/batch/batch-test"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {
                                "state": next(states),
                                "full_zip_url": "https://storage.example/result",
                            }
                        ]
                    },
                },
            )
        assert request.url.host == "storage.example"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if request.method == "PUT":
            assert request.content == source.read_bytes()
            assert int(request.headers["content-length"]) == source.stat().st_size
            return httpx.Response(200)
        return httpx.Response(200, content=result_zip())

    settings = Settings(data_dir=tmp_path, mineru_language="ch", mineru_is_ocr=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        auth=("injected", "secret"),
        headers={"Cookie": "injected-cookie"},
    ) as http:
        client = MinerUClient(settings, client=http)
        job = await client.submit_local_file(source, data_id="doc-test", page_ranges="1-2")
        result = await client.wait_for_result(job.batch_id, poll_interval=0)
        await client.download_and_extract(result, tmp_path / "raw")
    document = MinerUNormalizer().normalize(
        tmp_path / "raw",
        document_id="doc-test",
        parse_id="parse-test",
        source_sha256="a" * 64,
        source_filename="paper.pdf",
        provider_model="vlm",
    )
    assert document.title == "Synthetic title"
    assert document.pages[0].width == 600
    assert document.blocks[1].text == "Synthetic evidence."
    assert document.blocks[2].image_path == str(tmp_path / "raw/nested/images/figure.png")
    assert len(seen) == 8


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"batch_id": "", "file_urls": []},
        {"batch_id": "batch", "file_urls": ["one", "two"]},
    ],
)
async def test_invalid_upload_response_fails_before_upload(tmp_path, data):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"synthetic")
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"code": 0, "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MinerUClient(Settings(), client=http)
        with pytest.raises(MinerUError):
            await client.submit_local_file(source, data_id="test")
    assert calls == ["POST"]


@pytest.mark.parametrize(
    ("state", "match"), [("failed", "Parsing failed"), ("completed", "Unsupported cloud API")]
)
async def test_v1_or_failed_state_is_not_mistaken_for_v4_completion(state, match):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"extract_result": [{"state": state, "err_msg": "Parsing failed"}]},
                },
            )
        )
    ) as http:
        with pytest.raises(MinerUError, match=match):
            await MinerUClient(Settings(), client=http).wait_for_result("batch", poll_interval=0)


def test_product_version_is_not_cloud_model_version():
    with pytest.raises(ValidationError):
        Settings(mineru_model="4.0")
    with pytest.raises(MinerUError, match="separate adapter"):
        MinerUClient(Settings(mineru_base_url="http://localhost:8000/v1"))


def test_native_v4_middle_json_is_rejected_explicitly(tmp_path):
    (tmp_path / "middle_json.json").write_text(
        json.dumps({"schema": "docvortex.middle", "schema_version": "2.0", "pages": []})
    )
    with pytest.raises(ValueError, match="separate normalizer"):
        MinerUNormalizer().normalize(
            tmp_path,
            document_id="doc",
            parse_id="parse",
            source_sha256="a" * 64,
            source_filename="test.pdf",
            provider_model="vlm",
        )


async def test_format_failure_preserves_raw_output_and_failed_manifest(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scholarrag.ingestion.service import IngestionService

    settings = Settings(data_dir=tmp_path / "data")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"synthetic PDF")

    async def submit(*args, **kwargs):
        return SimpleNamespace(batch_id="batch", trace_id="test-trace")

    async def wait(*args, **kwargs):
        return {"code": 0, "data": {"extract_result": [{"state": "done"}]}}

    async def download(self, result, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "middle_json.json").write_text(
            '{"schema":"docvortex.middle","schema_version":"2.0","pages":[]}'
        )

    monkeypatch.setattr(MinerUClient, "submit_local_file", submit)
    monkeypatch.setattr(MinerUClient, "wait_for_result", wait)
    monkeypatch.setattr(MinerUClient, "download_and_extract", download)
    with pytest.raises(ValueError, match="separate normalizer"):
        await IngestionService(settings)._parse_mineru_candidate(source)
    manifest_path = next(settings.artifacts_dir.rglob("manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "failed" and manifest["error_type"] == "ValueError"
    assert (manifest_path.parent / "raw/mineru/middle_json.json").exists()
