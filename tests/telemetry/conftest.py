from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cfr_v2.telemetry import _state

_STATE_FIELDS = (
    "telemetry",
    "enabled",
    "initialised",
    "httpx_instrumented",
    "dev_exporter",
    "file_logger_configs",
    "httpx2_async_client",
    "httpx2_sync_client",
)


@pytest.fixture(autouse=True)
def reset_telemetry_state():
    """Telemetry state is module-global and init is idempotent; reset each test."""
    saved = {name: getattr(_state, name) for name in _STATE_FIELDS}
    _state.telemetry = None
    _state.enabled = False
    _state.initialised = False
    _state.httpx_instrumented = False
    _state.dev_exporter = False
    _state.file_logger_configs = {}
    _state.httpx2_async_client = None
    _state.httpx2_sync_client = None
    yield
    for name, value in saved.items():
        setattr(_state, name, value)


@pytest.fixture
def fake_elf_config():
    """A config object shaped like ee_config.load_config() output."""
    cfg = MagicMock()
    cfg.elf.service_name = "test-svc"
    return cfg
