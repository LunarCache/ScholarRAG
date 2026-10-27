from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from scholarrag.authorization import check_active_authorization
from scholarrag.config import Settings


class PaddleOCRAPIError(RuntimeError):
    pass


class PaddleOCRAPIClient:
    """Thin lazy wrapper around PaddleOCR's hosted official API SDK."""

    def __init__(self, settings: Settings) -> None:
        if settings.paddleocr_access_token is None:
            raise PaddleOCRAPIError("PADDLEOCR_ACCESS_TOKEN is not configured")
        self.token = settings.paddleocr_access_token.get_secret_value()
        self.model = settings.paddle_model

    @staticmethod
    def document_options(model: str) -> tuple[Any, dict[str, Any]]:
        """Build the same high-fidelity hosted pipeline options used by benchmarks."""
        try:
            from paddleocr import PaddleOCRVLOptions, PPStructureV3Options
        except ImportError as exc:
            raise PaddleOCRAPIError(
                "PaddleOCR SDK is optional; install with `uv sync --extra paddle`"
            ) from exc
        if model == "PaddleOCR-VL-1.6":
            options = PaddleOCRVLOptions(
                use_layout_detection=True,
                use_chart_recognition=True,
                use_ocr_for_image_block=True,
                temperature=0,
                format_block_content=True,
                prettify_markdown=False,
                return_markdown_images=True,
            )
        elif model == "PP-StructureV3":
            options = PPStructureV3Options(
                use_table_recognition=True,
                use_formula_recognition=True,
                use_chart_recognition=True,
                use_region_detection=True,
                format_block_content=True,
                prettify_markdown=False,
                return_markdown_images=True,
            )
        else:
            raise PaddleOCRAPIError(f"Unsupported PaddleOCR document model: {model}")
        return options, options.to_payload()

    async def parse_document(
        self,
        file_path: Path,
        *,
        model: str | None = None,
        options: Any = None,
        page_ranges: str | None = None,
    ) -> Any:
        try:
            from paddleocr import PaddleOCRClient
        except ImportError as exc:
            raise PaddleOCRAPIError(
                "PaddleOCR SDK is optional; install with `uv sync --extra paddle`"
            ) from exc

        def run() -> Any:
            check_active_authorization()
            client = PaddleOCRClient(token=self.token, request_timeout=300.0, poll_timeout=1200.0)
            try:
                return client.parse_document(
                    file_path=str(file_path),
                    model=model or self.model,
                    options=options,
                    page_ranges=page_ranges,
                )
            finally:
                client.close()

        return await asyncio.to_thread(run)

    async def parse_document_to_directory(
        self,
        file_path: Path,
        destination: Path,
        *,
        model: str | None = None,
        options: Any = None,
        page_ranges: str | None = None,
    ) -> tuple[Any, list[str]]:
        """Parse and download signed result resources before their URLs expire."""
        try:
            from paddleocr import PaddleOCRClient
        except ImportError as exc:
            raise PaddleOCRAPIError(
                "PaddleOCR SDK is optional; install with `uv sync --extra paddle`"
            ) from exc

        destination.mkdir(parents=True, exist_ok=True)

        def run() -> tuple[Any, list[str]]:
            check_active_authorization()
            client = PaddleOCRClient(token=self.token, request_timeout=300.0, poll_timeout=1200.0)
            try:
                result = client.parse_document(
                    file_path=str(file_path),
                    model=model or self.model,
                    options=options,
                    page_ranges=page_ranges,
                )
                resources = self._save_resources(client, result, destination)
                return result, resources
            finally:
                client.close()

        return await asyncio.to_thread(run)

    async def submit_document(
        self,
        file_path: Path,
        *,
        model: str | None = None,
        options: Any = None,
        page_ranges: str | None = None,
    ) -> Any:
        """Submit separately so the caller can persist the recoverable job ID."""
        try:
            from paddleocr import PaddleOCRClient
        except ImportError as exc:
            raise PaddleOCRAPIError(
                "PaddleOCR SDK is optional; install with `uv sync --extra paddle`"
            ) from exc

        def run() -> Any:
            check_active_authorization()
            client = PaddleOCRClient(token=self.token, request_timeout=300.0, poll_timeout=1200.0)
            try:
                return client.submit_document_parsing(
                    file_path=str(file_path),
                    model=model or self.model,
                    options=options,
                    page_ranges=page_ranges,
                )
            finally:
                client.close()

        return await asyncio.to_thread(run)

    async def wait_document(self, job_id: str) -> Any:
        try:
            from paddleocr import PaddleOCRClient
        except ImportError as exc:
            raise PaddleOCRAPIError(
                "PaddleOCR SDK is optional; install with `uv sync --extra paddle`"
            ) from exc

        def run() -> Any:
            check_active_authorization()
            client = PaddleOCRClient(token=self.token, request_timeout=300.0, poll_timeout=1200.0)
            try:
                return client.wait_document_parsing_result(job_id)
            finally:
                client.close()

        return await asyncio.to_thread(run)

    async def save_document_resources(self, result: Any, destination: Path) -> list[str]:
        """Download resources into per-page namespaces to avoid SDK filename collisions."""
        try:
            from paddleocr import PaddleOCRClient
        except ImportError as exc:
            raise PaddleOCRAPIError(
                "PaddleOCR SDK is optional; install with `uv sync --extra paddle`"
            ) from exc

        destination.mkdir(parents=True, exist_ok=True)

        def run() -> list[str]:
            check_active_authorization()
            client = PaddleOCRClient(token=self.token, request_timeout=300.0, poll_timeout=1200.0)
            try:
                return self._save_resources(client, result, destination)
            finally:
                client.close()

        return await asyncio.to_thread(run)

    async def save_payload_resources(self, payload: dict[str, Any], destination: Path) -> list[str]:
        """Recover resource downloads from a previously frozen API response."""
        try:
            from paddleocr import PaddleOCRClient
        except ImportError as exc:
            raise PaddleOCRAPIError(
                "PaddleOCR SDK is optional; install with `uv sync --extra paddle`"
            ) from exc

        destination.mkdir(parents=True, exist_ok=True)

        def run() -> list[str]:
            check_active_authorization()
            client = PaddleOCRClient(token=self.token, request_timeout=300.0, poll_timeout=1200.0)
            try:
                saved: list[str] = []
                for page_index, page in enumerate(payload.get("pages", []), start=1):
                    groups = {
                        "markdown": page.get("markdown_images") or {},
                        "output": page.get("output_images") or {},
                    }
                    saved.extend(
                        self._save_resource_groups(
                            client, groups, destination, page_index=page_index
                        )
                    )
                return saved
            finally:
                client.close()

        return await asyncio.to_thread(run)

    @staticmethod
    def _save_resources(client: Any, result: Any, destination: Path) -> list[str]:
        saved: list[str] = []
        for page_index, page in enumerate(result.pages, start=1):
            groups = {
                "markdown": page.markdown_images,
                "output": page.output_images,
            }
            saved.extend(
                PaddleOCRAPIClient._save_resource_groups(
                    client, groups, destination, page_index=page_index
                )
            )
        return saved

    @staticmethod
    def _save_resource_groups(
        client: Any,
        groups: dict[str, dict[str, str]],
        destination: Path,
        *,
        page_index: int,
    ) -> list[str]:
        saved: list[str] = []
        for group, resources in groups.items():
            for filename, resource_url in sorted(resources.items()):
                check_active_authorization()
                if not resource_url:
                    continue
                relative = PurePosixPath(filename)
                if relative.is_absolute() or not relative.name or ".." in relative.parts:
                    raise PaddleOCRAPIError(f"Unsafe PaddleOCR resource path: {filename!r}")
                target_dir = (
                    destination / f"page-{page_index:04d}" / group / Path(*relative.parts[:-1])
                )
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / relative.name
                if target.exists():
                    saved.append(str(target))
                    continue
                saved.append(
                    client.save_resource(
                        resource_url,
                        str(target_dir),
                        filename=relative.name,
                        overwrite=False,
                    )
                )
        return saved

    @staticmethod
    def to_dict(result: Any) -> dict[str, Any]:
        if is_dataclass(result) and not isinstance(result, type):
            return asdict(result)
        for name in ("model_dump", "to_dict", "dict"):
            method = getattr(result, name, None)
            if callable(method):
                return method()
        if hasattr(result, "__dict__"):
            return dict(result.__dict__)
        raise PaddleOCRAPIError(f"Unsupported PaddleOCR result type: {type(result)!r}")
