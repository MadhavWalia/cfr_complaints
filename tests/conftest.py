from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

# Stand-ins for private enterprise packages (ee-logging, ...) so tests that patch
# them can run without the internal Artifactory. Real packages take precedence.
_STUBS = Path(__file__).parent / "_stubs"
if str(_STUBS) not in sys.path:
    sys.path.append(str(_STUBS))

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Scrub OTEL_* env so header-capture defaults are deterministic per test."""
    for key in list(os.environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Block outbound connections in unit tests without breaking the event loop.

    ``integration`` tests are unrestricted; ``db`` tests may reach localhost only.
    """
    if "integration" in request.keywords:
        return
    allow_local = "db" in request.keywords
    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection

    def _is_local(address) -> bool:
        if isinstance(address, tuple) and address:
            return str(address[0]) in _LOCAL_HOSTS
        return isinstance(address, (str, bytes))  # unix sockets

    def _connect(self, address, *args, **kwargs):
        if allow_local and _is_local(address):
            return real_connect(self, address, *args, **kwargs)
        raise RuntimeError("network access is not allowed in unit tests")

    def _create_connection(address, *args, **kwargs):
        if allow_local and _is_local(address):
            return real_create_connection(address, *args, **kwargs)
        raise RuntimeError("network access is not allowed in unit tests")

    # Patch connect (not socket creation) so asyncio's self-pipe still works.
    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket, "create_connection", _create_connection)


@pytest.fixture
def tmp_logs(tmp_path):
    """A throwaway log directory so tests never touch logs/."""
    d = tmp_path / "logs"
    d.mkdir()
    return d
