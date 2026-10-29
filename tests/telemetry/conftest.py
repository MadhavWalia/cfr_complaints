from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from cfr_v2.telemetry import _state

_STATE_FIELDS = (
    "telemetry",
    "enabled",
    "initialised",
    "httpx_instrumented",
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
    _state.file_logger_configs = {}
    _state.httpx2_async_client = None
    _state.httpx2_sync_client = None
    yield
    for name, value in saved.items():
        setattr(_state, name, value)


@pytest.fixture(scope="session")
def _span_exporter():
    """One global in-memory provider for the session (OTel forbids re-setting it)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans(_span_exporter):
    """Finished spans captured during the test; cleared for isolation."""
    _span_exporter.clear()
    yield _span_exporter
    _span_exporter.clear()


@pytest.fixture
def fake_elf_config():
    """A config object shaped like ee_config.load_config() output."""
    cfg = MagicMock()
    cfg.elf.service_name = "test-svc"
    return cfg
