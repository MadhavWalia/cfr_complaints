from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from opentelemetry.trace import StatusCode

from cfr_v2.telemetry import (
    _state,
    add_event,
    context_from_traceparent,
    current_trace_id,
    current_traceparent,
    elf_headers,
    get_logger,
    get_meter,
    get_tracer,
    instruments,
    is_enabled,
    link_to_current,
    record_llm_call,
    set_attributes,
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


def test_span_records_allowlisted_attributes_with_prefix(spans):
    with span("op", rows=1, has_transcript=True):
        pass
    (recorded,) = spans.get_finished_spans()
    assert recorded.name == "op"
    assert recorded.attributes["cfr.rows"] == 1
    assert recorded.attributes["cfr.has_transcript"] is True
    assert recorded.status.status_code is StatusCode.UNSET


def test_span_drops_attributes_outside_the_allowlist(spans, caplog):
    with span("op", transcript="customer said something private", stage="c02.x", outcome=None):
        pass
    (recorded,) = spans.get_finished_spans()
    assert dict(recorded.attributes) == {"cfr.stage": "c02.x"}  # None values skipped too
    assert "customer said" not in caplog.text  # the warning names the key, never the value


def test_span_sets_error_status_and_reraises_without_the_message(spans):
    with pytest.raises(ValueError, match="boom"):
        with span("op", stage="s"):
            raise ValueError("boom: transcript text")
    (recorded,) = spans.get_finished_spans()
    assert recorded.status.status_code is StatusCode.ERROR
    assert recorded.status.description == "ValueError"
    (event,) = [e for e in recorded.events if e.name == "exception"]
    assert dict(event.attributes) == {"exception.type": "builtins.ValueError"}


def test_set_attributes_and_add_event_apply_the_allowlist(spans):
    with span("op") as s:
        set_attributes(s, outcome="ok", secret="x")
        add_event(s, "retry", attempt=2, message="x")
    (recorded,) = spans.get_finished_spans()
    assert dict(recorded.attributes) == {"cfr.outcome": "ok"}
    assert dict(recorded.events[0].attributes) == {"cfr.attempt": 2}


def test_traceparent_round_trip_parents_a_span_in_another_context(spans):
    with span("root", new_trace=True):
        traceparent = current_traceparent()
        trace_id = current_trace_id()
    assert traceparent is not None and trace_id in traceparent

    with span("child", context=context_from_traceparent(traceparent)):
        assert current_trace_id() == trace_id
    root, child = spans.get_finished_spans()
    assert child.parent.span_id == root.context.span_id


def test_context_from_traceparent_ignores_missing_or_invalid():
    assert context_from_traceparent(None) is None
    assert context_from_traceparent("not-a-traceparent") is None


def test_new_trace_links_back_to_the_caller(spans):
    with span("batch"):
        links = link_to_current()
        with span("case", new_trace=True, links=links):
            pass
    case, batch = spans.get_finished_spans()
    assert case.context.trace_id != batch.context.trace_id
    assert case.parent is None
    assert case.links[0].context.span_id == batch.context.span_id


def test_no_link_or_traceparent_without_an_active_span():
    assert link_to_current() == []
    assert current_traceparent() is None


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
