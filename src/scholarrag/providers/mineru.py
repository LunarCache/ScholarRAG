from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx

from scholarrag.authorization import check_active_authorization
from scholarrag.config import Settings


class MinerUError(RuntimeError):
    pass


@dataclass(frozen=True)
class MinerUJob:
    batch_id: str
    trace_id: str | None


class MinerUClient:
    """Hosted Precision API v4; not the MinerU 4.x self-hosted V1 API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if settings.mineru_api_token is None:
            raise MinerUError("MINERU_API_TOKEN is not configured")
        self.base_url = settings.mineru_base_url.rstrip("/")
        if httpx.URL(self.base_url).path.endswith("/v1"):
            raise MinerUError(
                "MinerU self-hosted V1 requires a separate adapter; this client uses cloud API v4"
            )
        self.model = settings.mineru_model
        self.language = settings.mineru_language
        self.is_ocr = settings.mineru_is_ocr
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(300.0))
        self.headers = {
            "Authorization": f"Bearer {settings.mineru_api_token.get_secret_value()}",
            "Content-Type": "application/json",
        }

    async def close(self) -> None:
        if self._owned_client:
            await self.client.aclose()

    async def submit_local_file(
        self,
        file_path: Path,
        *,
        data_id: str,
        page_ranges: str | None = None,
        on_allocated: Callable[[MinerUJob], None] | None = None,
    ) -> MinerUJob:
        check_active_authorization()
        file_spec: dict[str, Any] = {
            "name": file_path.name,
            "data_id": data_id,
            "is_ocr": self.is_ocr,
        }
        if page_ranges:
            file_spec["page_ranges"] = page_ranges
        payload = {
            "files": [file_spec],
            "model_version": self.model,
            "enable_formula": True,
            "enable_table": True,
            "language": self.language,
        }
        response = await self.client.post(
            f"{self.base_url}/file-urls/batch", headers=self.headers, json=payload, auth=None
        )
        response.raise_for_status()
        body = response.json()
        self._validate(body, "create upload URL")
        data = body.get("data") or {}
        urls = data.get("file_urls")
        batch_id = data.get("batch_id")
        if (
            not isinstance(batch_id, str)
            or not batch_id.strip()
            or not isinstance(urls, list)
            or len(urls) != 1
            or not isinstance(urls[0], str)
            or not urls[0].strip()
        ):
            raise MinerUError("Cloud API v4 returned invalid batch_id or file_urls")
        job = MinerUJob(batch_id=batch_id, trace_id=body.get("trace_id"))
        if on_allocated is not None:
            on_allocated(job)
        upload_url = urls[0]
        upload_response = await self._artifact_request(
            "PUT",
            upload_url,
            content=self._file_chunks(file_path),
            headers={"Content-Length": str(file_path.stat().st_size)},
        )
        upload_response.raise_for_status()
        return job

    async def _artifact_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        check_active_authorization()
        request = self.client.build_request(method, url, **kwargs)
        # Signed object-storage URLs authorize themselves. Do not merge client credentials.
        for header in ("Authorization", "Cookie", "Proxy-Authorization"):
            request.headers.pop(header, None)
        return await self.client.send(request, auth=None, follow_redirects=True)

    async def get_batch(self, batch_id: str) -> dict[str, Any]:
        check_active_authorization()
        response = await self.client.get(
            f"{self.base_url}/extract-results/batch/{batch_id}", headers=self.headers, auth=None
        )
        response.raise_for_status()
        body = response.json()
        self._validate(body, "query batch")
        if not isinstance(body["data"].get("extract_result"), list):
            raise MinerUError("Cloud API v4 returned invalid extract_result")
        return body

    async def wait_for_result(
        self,
        batch_id: str,
        *,
        poll_interval: float = 5.0,
        timeout: float = 1200.0,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            body = await self.get_batch(batch_id)
            results = body.get("data", {}).get("extract_result", [])
            if results:
                state = results[0].get("state")
                if state == "done":
                    return body
                if state == "failed":
                    raise MinerUError(results[0].get("err_msg") or "MinerU parsing failed")
                if state not in {"waiting-file", "pending", "running", "converting"}:
                    raise MinerUError(
                        "Unsupported cloud API task state; inspect the response contract"
                    )
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"MinerU batch {batch_id} did not finish in {timeout}s")
            await asyncio.sleep(poll_interval)

    async def download_and_extract(self, result: dict[str, Any], destination: Path) -> Path:
        rows = result.get("data", {}).get("extract_result", [])
        if not rows or not rows[0].get("full_zip_url"):
            raise MinerUError("Completed MinerU response has no full_zip_url")
        response = await self._artifact_request("GET", rows[0]["full_zip_url"])
        response.raise_for_status()
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            await asyncio.to_thread(self._safe_extract, archive, destination)
        return destination

    @staticmethod
    def _validate(body: dict[str, Any], operation: str) -> None:
        if not isinstance(body, dict):
            raise MinerUError("Invalid cloud API v4 JSON response")
        if body.get("code") != 0:
            raise MinerUError(f"MinerU failed to {operation}: {body.get('msg', 'unknown error')}")
        if not isinstance(body.get("data"), dict):
            raise MinerUError("Cloud API v4 returned invalid data")

    @staticmethod
    async def _file_chunks(path: Path, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        async with await anyio.open_file(path, "rb") as stream:
            while chunk := await stream.read(chunk_size):
                yield chunk

    @staticmethod
    def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
        root = destination.resolve()
        for member in archive.infolist():
            check_active_authorization()
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise MinerUError(f"Unsafe path in MinerU archive: {member.filename}")
        archive.extractall(destination)

    @staticmethod
    def summarize_response(body: dict[str, Any]) -> dict[str, Any]:
        """Redacted shape summary safe for logs and local smoke-test output."""
        rows = body.get("data", {}).get("extract_result", [])
        return {
            "code": body.get("code"),
            "trace_id": body.get("trace_id"),
            "result_count": len(rows),
            "results": [
                {
                    "file_name": row.get("file_name"),
                    "data_id": row.get("data_id"),
                    "state": row.get("state"),
                    "has_zip": bool(row.get("full_zip_url")),
                    "keys": sorted(row.keys()),
                }
                for row in rows
            ],
            "top_level_keys": sorted(body.keys()),
            "data_keys": sorted(body.get("data", {}).keys()),
        }
