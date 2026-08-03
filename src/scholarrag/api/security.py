from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.responses import JSONResponse, Response

from scholarrag.config import Settings


class LocalSecurityMiddleware:
    """Optional bearer authentication plus in-process per-client rate limiting."""

    def __init__(self, app: Callable[..., Awaitable[Response]], settings: Settings) -> None:
        self.app = app
        self.token = (
            settings.api_bearer_token.get_secret_value() if settings.api_bearer_token else None
        )
        self.limit = settings.api_requests_per_minute
        self.windows: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        if not request.url.path.startswith("/api/health"):
            if self.token:
                supplied = request.headers.get("authorization", "")
                expected = f"Bearer {self.token}"
                if not hmac.compare_digest(supplied, expected):
                    response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                    return
            client = request.client.host if request.client else "unknown"
            now = time.monotonic()
            window = self.windows[client]
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= self.limit:
                response = JSONResponse(
                    {"detail": "Rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
                await response(scope, receive, send)
                return
            window.append(now)
        await self.app(scope, receive, send)
