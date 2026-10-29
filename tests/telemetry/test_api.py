from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from opentelemetry.trace import StatusCode

from cfr_v2.telemetry import (
    _state,
    elf_headers,
    get_logger,
    get_meter,
    get_tracer,
    instruments,
    is_enabled,
    record_llm_call,
    span,
    telemetry,
)


# --- accessors ---------------------------------------------------------------


def test_telemetry_returns_state():
    assert telemetry() is None
    sentinel = object()
    _state.telemetry = sentinel
    assert telemetry() is sentinel


def test_current_span_context_handles_errors(monkeypatch):
    from cfr_v2.telemetry import api

    monkeypatch.setattr(
        api.trace, "get_current_span", MagicMock(side_effect=RuntimeError)
    )
    assert api._current_span_context() is None


def test_get_tracer_delegates_to_opentelemetry():
    assert type(get_tracer("x")).__module__.startswith("opentelemetry")


def test_get_meter_delegates_to_opentelemetry():
    assert type(get_meter("x")).__module__.startswith("opentelemetry")


def test_get_logger_stdlib_when_disabled():
    import logging

    logger = get_logger("some.logger")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "some.logger"


def test_get_logger_delegates_when_enabled(monkeypatch):
    _state.enabled = True
    sentinel = object()
    monkeypatch.setattr("ee_logging.get_logger", MagicMock(return_value=sentinel))
    assert get_logger("x") is sentinel


def test_is_enabled_reflects_state():
    assert is_enabled() is False
    _state.enabled = True
    assert is_enabled() is True


def test_instruments_none_when_disabled():
    assert instruments() is None


def test_instruments_returns_from_telemetry():
    _state.enabled = True
    inst = object()
    _state.telemetry = SimpleNamespace(instruments=inst)
    assert instruments() is inst


# --- span() ------------------------------------------------------------------


def test_span_records_attributes(spans):
    with span("op", num=1, flag=True):
        pass
    (recorded,) = spans.get_finished_spans()
    assert recorded.name == "op"
    assert recorded.attributes["num"] == 1
    assert recorded.attributes["flag"] is True
    assert recorded.status.status_code is StatusCode.UNSET


def test_span_sets_error_status_and_reraises(spans):
    with pytest.raises(ValueError, match="boom"):
        with span("op", k="v"):
            raise ValueError("boom")
    (recorded,) = spans.get_finished_spans()
    assert recorded.status.status_code is StatusCode.ERROR
    assert any(event.name == "exception" for event in recorded.events)


# --- elf_headers -------------------------------------------------------------


def test_elf_headers_passthrough_when_disabled():
    assert elf_headers({"x": "1"}) == {"x": "1"}
    assert elf_headers() == {}


def test_elf_headers_delegates_when_enabled(monkeypatch):
    _state.enabled = True

    def fake(existing):
        return {"b3": "1", **(existing or {})}

    monkeypatch.setattr("ee_logging.tracing.get_elf_headers", fake)
    out = elf_headers({"x": "1"})
    assert out == {"b3": "1", "x": "1"}


def test_elf_headers_falls_back_on_error(monkeypatch):
    _state.enabled = True
    monkeypatch.setattr(
        "ee_logging.tracing.get_elf_headers", MagicMock(side_effect=RuntimeError)
    )
    assert elf_headers({"x": "1"}) == {"x": "1"}


# --- record_llm_call ---------------------------------------------------------


def _enable_with_instruments():
    inst = SimpleNamespace(
        request_counter=MagicMock(),
        request_duration=MagicMock(),
        error_counter=MagicMock(),
    )
    _state.enabled = True
    _state.telemetry = SimpleNamespace(instruments=inst)
    return inst


def test_record_llm_call_success_records_metrics():
    inst = _enable_with_instruments()
    with record_llm_call("gpt-5", operation="chat"):
        pass
    inst.request_counter.add.assert_called_once()
    inst.request_duration.record.assert_called_once()
    inst.error_counter.add.assert_not_called()


def test_record_llm_call_error_records_error_and_reraises():
    inst = _enable_with_instruments()
    with pytest.raises(RuntimeError):
        with record_llm_call("gpt-5"):
            raise RuntimeError("x")
    inst.error_counter.add.assert_called_once()


def test_record_llm_call_noop_when_disabled():
    # No instruments configured; must not raise.
    with record_llm_call("gpt-5"):
        pass


# --- aobserve (async) --------------------------------------------------------


async def test_aobserve_passthrough_when_disabled():
    from cfr_v2.telemetry import aobserve

    @aobserve
    async def f(x):
        return x + 1

    assert await f(1) == 2


async def test_aobserve_wraps_when_enabled(monkeypatch):
    from cfr_v2.telemetry import aobserve

    _state.enabled = True
    marker = {}

    def fake_aobserve(fn):
        async def wrapper(*a, **k):
            marker["wrapped"] = True
            return await fn(*a, **k)

        return wrapper

    monkeypatch.setattr("ee_logging.aobserve", fake_aobserve)

    @aobserve
    async def f(x):
        return x * 2

    assert await f(3) == 6
    assert marker["wrapped"] is True
