"""Judge HTTP requests share provider capacity and recheck task-local authority."""

from __future__ import annotations

import httpx

from scholarrag.authorization import current_guard
from scholarrag.config import Settings
from scholarrag.providers.siliconflow import SiliconFlowClient


class AuthorizedJudgeTransport(httpx.AsyncBaseTransport):
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.provider = SiliconFlowClient(settings)
        self.transport = transport or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        current_guard(self.settings)
        async with self.provider._request_slot():
            current_guard(self.settings)
            response = await self.transport.handle_async_request(request)
            try:
                await response.aread()
                current_guard(self.settings)
                return response
            except BaseException:
                await response.aclose()
                raise

    async def aclose(self):
        await self.transport.aclose()
        await self.provider.close()
