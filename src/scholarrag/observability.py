"""Optional Langfuse export; local events remain the source of truth.

Only explicitly traced entry points export observations. ContextVar and the
SDK's OTel context keep concurrent requests and parallel retrieval isolated.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from scholarrag.config import Settings

logger = logging.getLogger(__name__)
_active: ContextVar[Telemetry | None] = ContextVar("scholarrag_telemetry", default=None)


def _content(value: Any, depth: int = 0) -> Any:
    """Bound opted-in content; never export embedded images or credentials."""
    if depth > 8:
        return "[truncated]"
    if isinstance(value, str):
        if "data:" in value or value.startswith(("http://", "https://")):
            return "[URL/media omitted]"
        return value[:6000]
    if isinstance(value, dict):
        return {
            str(key): _content(item, depth + 1)
            for key, item in list(value.items())[:50]
            if not any(
                term in str(key).lower()
                for term in (
                    "secret",
                    "token",
                    "authorization",
                    "api_key",
                    "image",
                    "path",
                    "reasoning",
                    "thinking",
                )
            )
        }
    if isinstance(value, (list, tuple)):
        return [_content(item, depth + 1) for item in value[:30]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[omitted]"


class Observation:
    def __init__(self, raw: Any = None, *, capture_content: bool = False) -> None:
        self.raw = raw
        self.capture_content = capture_content

    def update(self, **fields: Any) -> None:
        if self.raw is None:
            return
        for key in ("input", "output"):
            if key in fields:
                fields[key] = _content(fields[key]) if self.capture_content else None
        try:
            self.raw.update(**fields)
        except Exception:  # noqa: BLE001 - telemetry must not break business execution
            # Do not include SDK exceptions: they may contain payloads/credentials.
            logger.warning("Langfuse observation update failed; local execution continues")


@contextmanager
def _safe_scope(factory: Any) -> Iterator[Any]:
    """Ignore telemetry setup/teardown errors, never business exceptions."""
    try:
        scope = factory()
        value = scope.__enter__()
    except Exception:  # noqa: BLE001 - telemetry must not break business execution
        logger.warning("Langfuse scope initialization failed; local execution continues")
        yield None
        return
    try:
        yield value
    finally:
        try:
            # Report only exception types ourselves; prevent automatic raw error export.
            scope.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - telemetry must not break business execution
            logger.warning("Langfuse scope finalization failed; local execution continues")


class Telemetry:
    def __init__(self, settings: Settings) -> None:
        self.capture_content = settings.langfuse_capture_content
        self.client: Any = None
        if not settings.langfuse_enabled:
            return
        if not settings.langfuse_public_key or not settings.langfuse_secret_key:
            raise ValueError(
                "Langfuse enabled: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY required"
            )
        try:
            from langfuse import Langfuse
        except ImportError as exc:
            raise ValueError("Langfuse enabled: install uv sync --extra observability") from exc
        try:
            self.client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                base_url=settings.langfuse_base_url,
                environment=settings.langfuse_environment,
                timeout=5,
            )
        except Exception:  # noqa: BLE001 - telemetry must not break business execution
            logger.warning("Langfuse client initialization failed; export disabled")

    @contextmanager
    def observation(
        self, name: str, *, as_type: str = "span", **fields: Any
    ) -> Iterator[Observation]:
        if self.client is None:
            yield Observation()
            return
        for key in ("input", "output"):
            if key in fields:
                fields[key] = _content(fields[key]) if self.capture_content else None
        with _safe_scope(
            lambda: self.client.start_as_current_observation(name=name, as_type=as_type, **fields)
        ) as raw:
            observation = Observation(raw, capture_content=self.capture_content)
            try:
                yield observation
            except BaseException as exc:
                observation.update(level="ERROR", status_message=type(exc).__name__)
                raise

    @contextmanager
    def trace(
        self,
        name: str,
        *,
        trace_id: str,
        session_id: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        input: Any = None,
    ) -> Iterator[Observation]:
        token = _active.set(self)
        try:
            with self.observation(
                name,
                trace_context={"trace_id": trace_id},
                metadata=metadata,
                input=input,
            ) as root:
                if self.client is None:
                    yield root
                else:
                    from langfuse import propagate_attributes

                    # Public session IDs are not trusted as real user identities.
                    session = session_id
                    if session and (not session.isascii() or len(session) > 200):
                        session = hashlib.sha256(session.encode()).hexdigest()
                    dimensions = (
                        {"workspace_id": metadata["workspace_id"]}
                        if metadata and metadata.get("workspace_id")
                        else None
                    )
                    with _safe_scope(
                        lambda: propagate_attributes(
                            session_id=session, user_id=user_id, metadata=dimensions
                        )
                    ):
                        yield root
        finally:
            _active.reset(token)

    def shutdown(self) -> None:
        if self.client is not None:
            try:
                self.client.shutdown()
            except Exception:  # noqa: BLE001 - telemetry must not break business execution
                logger.warning("Langfuse shutdown failed; pending telemetry may be lost")


@contextmanager
def observe(name: str, *, as_type: str = "span", **fields: Any) -> Iterator[Observation]:
    telemetry = _active.get()
    if telemetry is None:
        yield Observation()
    else:
        with telemetry.observation(name, as_type=as_type, **fields) as observation:
            yield observation


def usage_details(usage: dict[str, Any]) -> dict[str, int]:
    """Normalize OpenAI-compatible usage without counting total twice."""
    result = {}
    for source, target in (("prompt_tokens", "input"), ("completion_tokens", "output")):
        value = usage.get(source)
        if isinstance(value, int) and value >= 0:
            result[target] = value
    if not result and isinstance(usage.get("total_tokens"), int):
        result["total"] = usage["total_tokens"]
    return result
