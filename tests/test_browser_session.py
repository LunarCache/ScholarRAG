from __future__ import annotations

import httpx
import pytest
from test_workspace_identity import team as team  # noqa: PLC0414

from scholarrag.api.main import create_app


@pytest.mark.parametrize(
    "origin,expected", [("https://scholar.example", 200), ("https://other.example", 400)]
)
async def test_browser_preflight_allows_only_configured_origin(team, origin, expected):
    team.settings.api_cors_origins = ["https://scholar.example"]
    app = create_app(team.settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.options(
            f"/api/workspaces/{team.workspace}/uploads",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Idempotency-Key",
            },
        )
    assert response.status_code == expected
    if expected == 200:
        assert response.headers["access-control-allow-origin"] == origin
    else:
        assert "access-control-allow-origin" not in response.headers


async def test_browser_can_read_authentication_failure_and_logout_revokes_token(team):
    app = create_app(team.settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Origin": "http://localhost:3000"},
    ) as client:
        rejected = await client.get("/api/me")
        assert rejected.status_code == 401
        assert rejected.headers["access-control-allow-origin"] == "http://localhost:3000"
        client.headers["Authorization"] = f"Bearer {team.sessions['member'][1]}"
        assert (await client.get("/api/me")).status_code == 200
        response = await client.delete("/api/me/session")
        assert response.status_code == 204 and "no-store" in response.headers["cache-control"]
        assert (await client.get("/api/workspaces")).status_code == 401


def test_import_time_default_application_uses_disposable_data_directory():
    from pathlib import Path

    from scholarrag.api.main import app

    root = app.state.runtime.settings.data_dir.resolve()
    assert root != Path("data").resolve()
    assert root.name.startswith("scholarrag-pytest-")


@pytest.mark.parametrize("mode", ["local", "session"])
async def test_public_health_exposes_only_the_mode_needed_for_browser_entry(team, mode):
    app = create_app(team.settings.model_copy(update={"auth_mode": mode}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "auth_mode": mode}
