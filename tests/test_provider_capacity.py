from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from scholarrag.config import Settings
from scholarrag.providers.siliconflow import SiliconFlowClient, SiliconFlowError


async def test_multiple_clients_share_capacity_for_stream_and_post(tmp_path):
    settings = Settings(data_dir=tmp_path, provider_max_concurrency=1)
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content).get("stream", False))
        return httpx.Response(200, json={"choices": [], "usage": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        first = SiliconFlowClient(settings, client=http)
        second = SiliconFlowClient(settings, client=http)
        async with first.chat_stream([]):
            pending = asyncio.create_task(second.chat([]))
            await asyncio.sleep(0.02)
            assert calls == [True]
            assert not pending.done()
        await pending
        assert calls == [True, False]


async def test_queue_timeout_and_cancel_do_not_leak_slots(tmp_path):
    settings = Settings(
        data_dir=tmp_path, provider_max_concurrency=1, provider_queue_timeout_seconds=0.02
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as http:
        first = SiliconFlowClient(settings, client=http)
        second = SiliconFlowClient(settings, client=http)
        async with first.chat_stream([]):
            with pytest.raises(SiliconFlowError, match="queue timed out"):
                await second.chat([])
            task = asyncio.create_task(second.chat([]))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(second.chat([]), timeout=1)
