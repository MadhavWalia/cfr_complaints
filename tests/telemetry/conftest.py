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
    "early_loggers",
    "httpx2_async_client",
    "httpx2_sync_client",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Each test starts with no ee config and no dev endpoint, whatever the machine has.

    Where the real ee-config and ee-logging are installed and a config file loads, or
    ``CFR_OTLP_ENDPOINT`` is set in the shell, ``init_telemetry`` would otherwise start real
    telemetry. Tests that need either set it themselves.
    """
    monkeypatch.setattr("ee_config.load_config", MagicMock(side_effect=RuntimeError("no ee config in tests")))
    monkeypatch.delenv("CFR_OTLP_ENDPOINT", raising=False)


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
    _state.early_loggers = {}
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
