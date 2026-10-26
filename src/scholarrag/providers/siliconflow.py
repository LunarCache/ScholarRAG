from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from weakref import WeakKeyDictionary, WeakValueDictionary

import httpx

from scholarrag.authorization import check_active_authorization, current_guard
from scholarrag.config import Settings
from scholarrag.observability import Observation, observe, usage_details
from scholarrag.storage.cache import ProviderCache

_LIMITERS: WeakKeyDictionary = WeakKeyDictionary()


class SiliconFlowError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderResponse:
    data: dict[str, Any]
    trace_id: str | None


class ProviderStream:
    def __init__(self, response: httpx.Response, observation: Observation | None = None) -> None:
        self.response = response
        self.trace_id = response.headers.get("x-siliconcloud-trace-id")
        self.usage: dict[str, Any] = {}
        self.observation = observation or Observation()
        self.text = ""
        self._completion_started = False

    async def chunks(self) -> AsyncIterator[dict[str, Any]]:
        async for line in self.response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            payload = json.loads(data)
            check_active_authorization()
            if isinstance(payload.get("usage"), dict):
                self.usage = payload["usage"]
            for choice in payload.get("choices") or []:
                delta = choice.get("delta") or {}
                if not self._completion_started and (
                    delta.get("content")
                    or delta.get("reasoning_content")
                    or delta.get("tool_calls")
                ):
                    self._completion_started = True
                    self.observation.update(completion_start_time=datetime.now(UTC))
                content = delta.get("content")
                if self.observation.capture_content and isinstance(content, str):
                    self.text = (self.text + content)[:6000]
            yield payload


class SiliconFlowClient:
    _failure_count = 0
    _circuit_open_until = 0.0

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if settings.siliconflow_api_key is None:
            raise SiliconFlowError("SILICONFLOW_API_KEY is not configured")
        self.base_url = settings.siliconflow_base_url.rstrip("/")
        self.settings = settings
        self.embedding_model = settings.unified_embedding_model
        self.multimodal_embedding_model = settings.multimodal_embedding_model
        self.embedding_dimensions = settings.embedding_dimensions
        self.rerank_model = settings.rerank_model
        self.chat_model = settings.chat_model
        self.chat_enable_thinking = settings.chat_enable_thinking
        self.chat_temperature = settings.chat_temperature
        self.chat_top_p = settings.chat_top_p
        self.chat_top_k = settings.chat_top_k
        self.chat_min_p = settings.chat_min_p
        self.chat_thinking_budget = settings.chat_thinking_budget
        self.max_retries = settings.provider_max_retries
        self.failure_threshold = settings.provider_circuit_failure_threshold
        self.circuit_cooldown = settings.provider_circuit_cooldown_seconds
        self._limit = settings.provider_max_concurrency
        self._queue_timeout = settings.provider_queue_timeout_seconds
        self._limiter_key = (
            self.base_url,
            hashlib.sha256(settings.siliconflow_api_key.get_secret_value().encode()).hexdigest(),
            self._limit,
        )
        self.cache = ProviderCache(settings)
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(180.0))
        self.headers = {
            "Authorization": f"Bearer {settings.siliconflow_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

    async def close(self) -> None:
        if self._owned_client:
            await self.client.aclose()

    @asynccontextmanager
    async def _request_slot(self) -> AsyncIterator[None]:
        # Share capacity across client instances; never share asyncio primitives across loops.
        loop = asyncio.get_running_loop()
        registry = _LIMITERS.setdefault(loop, WeakValueDictionary())
        semaphore = registry.get(self._limiter_key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._limit)
            registry[self._limiter_key] = semaphore
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=self._queue_timeout)
        except TimeoutError as exc:
            raise SiliconFlowError("SiliconFlow concurrency queue timed out") from exc
        try:
            check_active_authorization()
            yield
        finally:
            semaphore.release()

    async def embeddings(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> ProviderResponse:
        current_guard(self.settings)
        active_model = model or self.embedding_model
        active_dimensions = dimensions or self.embedding_dimensions
        return await self._cached_embeddings(
            inputs=texts,
            model=active_model,
            payload={
                "model": active_model,
                "input": texts,
                "dimensions": active_dimensions,
                "encoding_format": "float",
                "truncate": "right",
            },
            dimensions=active_dimensions,
        )

    async def multimodal_embeddings(
        self,
        inputs: list[str | dict[str, str]],
        *,
        dimensions: int | None = None,
    ) -> ProviderResponse:
        current_guard(self.settings)
        active_dimensions = dimensions or self.embedding_dimensions
        payload = {
            "model": self.multimodal_embedding_model,
            "input": inputs,
            "dimensions": active_dimensions,
            "encoding_format": "float",
            "truncate": "right",
        }
        return await self._cached_embeddings(
            inputs=inputs,
            model=self.multimodal_embedding_model,
            payload=payload,
            dimensions=active_dimensions,
        )

    async def rerank(
        self,
        query: str | dict[str, str],
        documents: list[str | dict[str, str]],
        *,
        top_n: int | None = None,
    ) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": self.rerank_model,
            "query": query,
            "documents": documents,
            "return_documents": False,
        }
        if top_n is not None:
            payload["top_n"] = top_n
        return await self._post("/rerank", payload)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        thinking_budget: int | None = None,
        response_format: dict[str, str] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": messages,
            "temperature": (self.chat_temperature if temperature is None else temperature),
            "enable_thinking": self.chat_enable_thinking,
        }
        self._apply_sampling_params(payload, top_p=top_p, top_k=top_k, min_p=min_p)
        if thinking_budget is not None:
            payload["thinking_budget"] = thinking_budget
        if response_format:
            payload["response_format"] = response_format
        if tools:
            payload["tools"] = tools
        return await self._post("/chat/completions", payload)

    @asynccontextmanager
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        thinking_budget: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderStream]:
        current_guard(self.settings)
        payload = {
            "model": self.chat_model,
            "messages": messages,
            "temperature": (self.chat_temperature if temperature is None else temperature),
            "enable_thinking": self.chat_enable_thinking,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        self._apply_sampling_params(payload, top_p=top_p, top_k=top_k, min_p=min_p)
        if thinking_budget is not None:
            payload["thinking_budget"] = thinking_budget
        if tools:
            payload["tools"] = tools
        with observe(
            "siliconflow.chat.stream",
            as_type="generation",
            model=self.chat_model,
            input=messages,
            model_parameters={"temperature": payload["temperature"]},
            metadata={"provider": "siliconflow", "stream": True},
        ) as observation:
            async with (
                self._request_slot(),
                self.client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json=payload,
                ) as response,
            ):
                if response.is_error:
                    body = (await response.aread()).decode(errors="replace")
                    raise SiliconFlowError(
                        f"SiliconFlow /chat/completions failed ({response.status_code}): {body[:500]}"
                    )
                stream = ProviderStream(response, observation)
                try:
                    yield stream
                finally:
                    observation.update(
                        output=stream.text or None,
                        usage_details=usage_details(stream.usage),
                        metadata={"provider_trace_id": stream.trace_id},
                    )

    def _apply_sampling_params(
        self,
        payload: dict[str, Any],
        *,
        top_p: float | None,
        top_k: int | None,
        min_p: float | None,
    ) -> None:
        """Apply Qwen3 official sampling defaults unless overridden per call."""
        payload["top_p"] = self.chat_top_p if top_p is None else top_p
        payload["top_k"] = self.chat_top_k if top_k is None else top_k
        if min_p is not None or self.chat_min_p > 0:
            payload["min_p"] = self.chat_min_p if min_p is None else min_p

    async def list_models(self) -> ProviderResponse:
        response = await self.client.get(f"{self.base_url}/models", headers=self.headers)
        response.raise_for_status()
        return ProviderResponse(response.json(), response.headers.get("x-siliconcloud-trace-id"))

    async def _post(self, path: str, payload: dict[str, Any]) -> ProviderResponse:
        current_guard(self.settings)
        kind = (
            "generation"
            if path == "/chat/completions"
            else "embedding"
            if path == "/embeddings"
            else "span"
        )
        with observe(
            f"siliconflow{path}",
            as_type=kind,
            **({"model": payload.get("model")} if kind != "span" else {}),
            input=payload.get("messages") if kind == "generation" else None,
            metadata={"provider": "siliconflow", "model": payload.get("model")},
        ) as observation:
            response = await self._post_with_retries(path, payload)
            check_active_authorization()
            fields: dict[str, Any] = {
                "metadata": {"provider_trace_id": response.trace_id, "provider": "siliconflow"},
            }
            if kind in {"generation", "embedding"}:
                fields["usage_details"] = usage_details(response.data.get("usage") or {})
            if kind == "generation":
                fields["output"] = [
                    choice.get("message", {}).get("content")
                    for choice in response.data.get("choices") or []
                ]
            observation.update(**fields)
            return response

    async def _post_with_retries(self, path: str, payload: dict[str, Any]) -> ProviderResponse:
        if time.monotonic() < type(self)._circuit_open_until:
            raise SiliconFlowError("SiliconFlow circuit is temporarily open after repeated errors")
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            check_active_authorization()
            try:
                async with self._request_slot():
                    response = await self.client.post(
                        f"{self.base_url}{path}", headers=self.headers, json=payload
                    )
                if not response.is_error:
                    type(self)._failure_count = 0
                    return ProviderResponse(
                        response.json(), response.headers.get("x-siliconcloud-trace-id")
                    )
                last_error = f"HTTP {response.status_code}: {response.text[:500]}"
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt >= self.max_retries:
                    break
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after else min(8.0, 0.5 * 2**attempt)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self.max_retries:
                    break
                delay = min(8.0, 0.5 * 2**attempt)
            await asyncio.sleep(delay + random.random() * 0.15)
        type(self)._failure_count += 1
        if type(self)._failure_count >= self.failure_threshold:
            type(self)._circuit_open_until = time.monotonic() + self.circuit_cooldown
        raise SiliconFlowError(f"SiliconFlow {path} failed after retries: {last_error}")

    async def _cached_embeddings(
        self,
        *,
        inputs: list[Any],
        model: str,
        payload: dict[str, Any],
        dimensions: int,
    ) -> ProviderResponse:
        operation = f"embedding:{dimensions}"
        keys = [self.cache.key("siliconflow", model, operation, item) for item in inputs]
        vectors: list[list[float] | None] = [self.cache.get(key) for key in keys]
        missing_indexes = [index for index, vector in enumerate(vectors) if vector is None]
        trace_id: str | None = None
        usage: dict[str, Any] = {}
        if missing_indexes:
            missing_inputs = [inputs[index] for index in missing_indexes]
            request_payload = {**payload, "input": missing_inputs}
            response = await self._post("/embeddings", request_payload)
            trace_id = response.trace_id
            usage = response.data.get("usage") or {}
            rows = response.data.get("data") or []
            if len(rows) != len(missing_indexes):
                raise SiliconFlowError("Embedding response cardinality does not match request")
            for index, row in zip(missing_indexes, rows, strict=True):
                vector = row["embedding"]
                vectors[index] = vector
                self.cache.put(
                    keys[index],
                    provider="siliconflow",
                    model=model,
                    operation=operation,
                    value=vector,
                )
        return ProviderResponse(
            {
                "model": model,
                "data": [
                    {"index": index, "embedding": vector} for index, vector in enumerate(vectors)
                ],
                "usage": usage,
                "cache": {
                    "hits": len(inputs) - len(missing_indexes),
                    "misses": len(missing_indexes),
                },
            },
            trace_id,
        )
