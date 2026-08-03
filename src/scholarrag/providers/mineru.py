from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx

from scholarrag.config import Settings


class MinerUError(RuntimeError):
    pass


@dataclass(frozen=True)
class MinerUJob:
    batch_id: str
    trace_id: str | None


class MinerUClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if settings.mineru_api_token is None:
            raise MinerUError("MINERU_API_TOKEN is not configured")
        self.base_url = settings.mineru_base_url.rstrip("/")
        self.model = settings.mineru_model
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
    ) -> MinerUJob:
        file_spec: dict[str, Any] = {"name": file_path.name, "data_id": data_id}
        if page_ranges:
            file_spec["page_ranges"] = page_ranges
        payload = {
            "files": [file_spec],
            "model_version": self.model,
            "enable_formula": True,
            "enable_table": True,
            "language": "en",
        }
        response = await self.client.post(
            f"{self.base_url}/file-urls/batch", headers=self.headers, json=payload
        )
        response.raise_for_status()
        body = response.json()
        self._validate(body, "create upload URL")
        upload_url = body["data"]["file_urls"][0]
        upload_response = await self.client.put(
            upload_url,
            content=self._file_chunks(file_path),
            headers={"Content-Length": str(file_path.stat().st_size)},
        )
        upload_response.raise_for_status()
        return MinerUJob(batch_id=body["data"]["batch_id"], trace_id=body.get("trace_id"))

    async def get_batch(self, batch_id: str) -> dict[str, Any]:
        response = await self.client.get(
            f"{self.base_url}/extract-results/batch/{batch_id}", headers=self.headers
        )
        response.raise_for_status()
        body = response.json()
        self._validate(body, "query batch")
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
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"MinerU batch {batch_id} did not finish in {timeout}s")
            await asyncio.sleep(poll_interval)

    async def download_and_extract(self, result: dict[str, Any], destination: Path) -> Path:
        rows = result.get("data", {}).get("extract_result", [])
        if not rows or not rows[0].get("full_zip_url"):
            raise MinerUError("Completed MinerU response has no full_zip_url")
        response = await self.client.get(rows[0]["full_zip_url"])
        response.raise_for_status()
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self._safe_extract(archive, destination)
        return destination

    @staticmethod
    def _validate(body: dict[str, Any], operation: str) -> None:
        if body.get("code") != 0:
            raise MinerUError(f"MinerU failed to {operation}: {body.get('msg', 'unknown error')}")

    @staticmethod
    async def _file_chunks(path: Path, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        async with await anyio.open_file(path, "rb") as stream:
            while chunk := await stream.read(chunk_size):
                yield chunk

    @staticmethod
    def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
        root = destination.resolve()
        for member in archive.infolist():
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
