from __future__ import annotations

import asyncio
import hmac
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.responses import JSONResponse, Response

from scholarrag.config import Settings
from scholarrag.domain.access import AccessDenied
from scholarrag.storage.identity import IdentityStore


class LocalSecurityMiddleware:
    """Optional bearer authentication plus in-process per-client rate limiting."""

    def __init__(
        self,
        app: Callable[..., Awaitable[Response]],
        settings: Settings,
        identities: IdentityStore | None = None,
    ) -> None:
        self.app = app
        self.mode = settings.auth_mode
        self.identities = identities
        self.token = (
            settings.api_bearer_token.get_secret_value() if settings.api_bearer_token else None
        )
        self.limit = settings.api_requests_per_minute
        self.windows: dict[str, deque[float]] = defaultdict(deque)
        self._last_cleanup = 0.0

    def _admit(self, key: str) -> bool:
        now = time.monotonic()
        if now - self._last_cleanup >= 60:
            for stale in [
                k for k, values in self.windows.items() if not values or values[-1] <= now - 60
            ]:
                del self.windows[stale]
            self._last_cleanup = now
        window = self.windows.get(key)
        if window is None:
            if len(self.windows) >= 10_000:
                return False
            window = deque()
            self.windows[key] = window
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) >= self.limit:
            return False
        window.append(now)
        return True

    @staticmethod
    def _limited_response() -> JSONResponse:
        return JSONResponse(
            {"detail": "Rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"}
        )

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        if request.url.path not in {"/api/health", "/api/health/ready"}:
            client = request.client.host if request.client else "unknown"
            if self.mode == "session" and request.url.path.startswith("/api/"):
                if not self._admit(f"ip:{client}"):
                    await self._limited_response()(scope, receive, send)
                    return
                supplied = request.headers.get("authorization", "")
                try:
                    if not supplied.startswith("Bearer ") or self.identities is None:
                        raise AccessDenied("Authentication required")
                    principal = await asyncio.to_thread(self.identities.authenticate, supplied[7:])
                except AccessDenied:
                    response = JSONResponse({"detail": "Authentication required"}, status_code=401)
                    await response(scope, receive, send)
                    return
                scope.setdefault("state", {})["principal"] = principal
                client = f"user:{principal.user_id}"
                # Until migrated, legacy endpoints must not expose the shared personal library.
                if not (
                    request.url.path == "/api/me"
                    or request.url.path == "/api/me/session"
                    or request.url.path == "/api/workspaces"
                    or request.url.path.startswith("/api/workspaces/")
                ):
                    response = JSONResponse(
                        {"detail": "This operation is not available in session mode yet"},
                        status_code=501,
                    )
                    await response(scope, receive, send)
                    return
            elif self.mode == "local" and self.token:
                supplied = request.headers.get("authorization", "")
                expected = f"Bearer {self.token}"
                if not hmac.compare_digest(supplied, expected):
                    response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                    return
            if not self._admit(client):
                await self._limited_response()(scope, receive, send)
                return

        async def send_private(message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ]
                message = {
                    **message,
                    "headers": [*headers, (b"cache-control", b"private, no-store")],
                }
            await send(message)

        await self.app(
            scope,
            receive,
            send_private
            if self.mode == "session" and request.url.path.startswith("/api/")
            else send,
        )
