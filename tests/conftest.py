"""Hermetic test defaults: no private .env or paid provider connections."""

import socket
import tempfile

import pytest

from scholarrag.config import Settings

_collection_patch = pytest.MonkeyPatch()
_collection_data: tempfile.TemporaryDirectory | None = None


def pytest_configure() -> None:
    global _collection_data
    # Collection imports api.main, which creates the default app immediately.
    # Isolate configuration before those imports, not just before test functions.
    _collection_patch.setitem(Settings.model_config, "env_file", None)
    _collection_data = tempfile.TemporaryDirectory(prefix="scholarrag-pytest-")
    _collection_patch.setenv("SCHOLARRAG_DATA_DIR", _collection_data.name)
    for key in ("MINERU_API_TOKEN", "PADDLEOCR_ACCESS_TOKEN", "SILICONFLOW_API_KEY"):
        _collection_patch.setenv(key, "test-not-a-real-key")
    _collection_patch.setenv("SCHOLARRAG_LANGFUSE_ENABLED", "false")
    _collection_patch.setenv("SCHOLARRAG_AUTH_MODE", "local")


def pytest_unconfigure() -> None:
    _collection_patch.undo()
    if _collection_data is not None:
        _collection_data.cleanup()


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key in ("MINERU_API_TOKEN", "PADDLEOCR_ACCESS_TOKEN", "SILICONFLOW_API_KEY"):
        monkeypatch.setenv(key, "test-not-a-real-key")
    monkeypatch.setenv("SCHOLARRAG_LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("SCHOLARRAG_AUTH_MODE", "local")

    def deny_network(*args, **kwargs):
        raise RuntimeError("Network disabled in tests; use a mock transport")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
