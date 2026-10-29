from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.trace import SpanKind, StatusCode

from cfr_v2.telemetry import _state, safechain_httpx_kwargs
from cfr_v2.telemetry import http as http_mod
from cfr_v2.telemetry.http import (
    InstrumentedHttpx2AsyncClientMixin,
    InstrumentedHttpx2SyncClientMixin,
    _capture_http_headers,
    _httpx_header_size,
)


# --- fakes -------------------------------------------------------------------


class FakeURL:
    host = "api.example.com"
    port = 443
    scheme = "https"
    path = "/v1/chat"


class FakeHeaders(dict):
    def multi_items(self):
        return list(self.items())


class FakeRequest:
    def __init__(self, method="POST"):
        self.method = method
        self.url = FakeURL()
        self.headers = FakeHeaders(
            {"content-type": "application/json", "authorization": "Bearer secret"}
        )


class FakeResponse:
    def __init__(self, status=200):
        self.status_code = status
        self.headers = FakeHeaders({"content-type": "application/json"})


class _AsyncBase:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    async def send(self, request, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return self._response


class _SyncBase:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def send(self, request, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return self._response


class InstrAsync(InstrumentedHttpx2AsyncClientMixin, _AsyncBase):
    pass


class InstrSync(InstrumentedHttpx2SyncClientMixin, _SyncBase):
    pass


# --- span recording ----------------------------------------------------------


async def test_async_send_records_client_span_2xx(spans):
    _state.enabled = True
    client = InstrAsync(response=FakeResponse(200))
    resp = await client.send(FakeRequest("POST"))

    assert resp.status_code == 200
    (recorded,) = spans.get_finished_spans()
    assert recorded.kind is SpanKind.CLIENT
    assert recorded.name == "HTTP POST"
    assert recorded.attributes["http.request.method"] == "POST"
    assert recorded.attributes["server.address"] == "api.example.com"
    assert recorded.attributes["http.response.status_code"] == 200
    assert recorded.status.status_code is StatusCode.UNSET


@pytest.mark.parametrize(
    "status,expected",
    [(200, StatusCode.UNSET), (404, StatusCode.ERROR), (500, StatusCode.ERROR)],
)
def test_sync_send_span_status(spans, status, expected):
    _state.enabled = True
    client = InstrSync(response=FakeResponse(status))
    client.send(FakeRequest())

    (recorded,) = spans.get_finished_spans()
    assert recorded.attributes["http.response.status_code"] == status
    assert recorded.status.status_code is expected


async def test_async_send_records_exception(spans):
    _state.enabled = True
    client = InstrAsync(error=ValueError("boom"))
    with pytest.raises(ValueError, match="boom"):
        await client.send(FakeRequest())

    (recorded,) = spans.get_finished_spans()
    assert recorded.status.status_code is StatusCode.ERROR
    assert recorded.attributes["error.type"] == "ValueError"
    assert any(event.name == "exception" for event in recorded.events)


def test_sync_send_records_exception(spans):
    _state.enabled = True
    client = InstrSync(error=RuntimeError("nope"))
    with pytest.raises(RuntimeError, match="nope"):
        client.send(FakeRequest())

    (recorded,) = spans.get_finished_spans()
    assert recorded.status.status_code is StatusCode.ERROR
    assert recorded.attributes["error.type"] == "RuntimeError"


def test_span_suppressed_when_disabled(spans):
    _state.enabled = False
    client = InstrSync(response=FakeResponse(200))
    client.send(FakeRequest())
    assert spans.get_finished_spans() == ()


async def test_async_span_suppressed_when_disabled(spans):
    _state.enabled = False
    client = InstrAsync(response=FakeResponse(200))
    await client.send(FakeRequest())
    assert spans.get_finished_spans() == ()


# --- header helpers ----------------------------------------------------------


def test_capture_http_headers_redacts_sanitized():
    class Rec:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, key, value):
            self.attrs[key] = value

    rec = Rec()
    headers = [("Authorization", "secret"), ("Content-Type", "application/json")]
    _capture_http_headers(
        rec, headers, "http.request.header", ["authorization", "content-type"], ["authorization"]
    )
    assert rec.attrs["http.request.header.authorization"] == "[REDACTED]"
    assert rec.attrs["http.request.header.content_type"] == "application/json"


def test_capture_http_headers_no_patterns_is_noop():
    rec = MagicMock()
    _capture_http_headers(rec, {"a": "b"}, "prefix", [], [])
    rec.set_attribute.assert_not_called()


def test_httpx_header_size():
    assert _httpx_header_size({"content-length": "42"}) == 42
    assert _httpx_header_size(None) is None
    assert _httpx_header_size({}) is None


def test_httpx_header_size_from_iterable():
    assert _httpx_header_size([("content-length", "7")]) == 7


def test_set_request_attributes_handles_missing_fields():
    rec = MagicMock()
    http_mod._set_httpx_request_attributes(
        rec, SimpleNamespace(method=None, url=None, headers=None)
    )  # must not raise


def test_capture_http_headers_decodes_bytes():
    class Rec:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, key, value):
            self.attrs[key] = value

    rec = Rec()
    _capture_http_headers(rec, [(b"X-Token", b"abc")], "http.request.header", ["x-token"], [])
    assert rec.attrs["http.request.header.x_token"] == "abc"


# --- client factories --------------------------------------------------------


def test_get_instrumented_clients_cached(monkeypatch):
    calls = {"n": 0}
    sentinel_async = object()
    sentinel_sync = object()

    def fake_make():
        calls["n"] += 1
        return sentinel_async, sentinel_sync

    monkeypatch.setattr(http_mod, "_make_httpx2_clients", fake_make)
    assert http_mod.get_instrumented_httpx2_async_client() is sentinel_async
    assert http_mod.get_instrumented_httpx2_async_client() is sentinel_async
    assert http_mod.get_instrumented_httpx2_sync_client() is sentinel_sync
    assert calls["n"] == 1


def test_safechain_httpx_kwargs(monkeypatch):
    sentinel_async = object()
    sentinel_sync = object()
    monkeypatch.setattr(
        http_mod, "_make_httpx2_clients", lambda: (sentinel_async, sentinel_sync)
    )
    kwargs = safechain_httpx_kwargs()
    assert kwargs == {
        "http_client": sentinel_sync,
        "http_async_client": sentinel_async,
    }


def test_make_httpx2_clients_real():
    pytest.importorskip("openai")
    pytest.importorskip("httpx2")
    import asyncio

    async_client, sync_client = http_mod._make_httpx2_clients()
    try:
        assert isinstance(async_client, InstrumentedHttpx2AsyncClientMixin)
        assert isinstance(sync_client, InstrumentedHttpx2SyncClientMixin)
    finally:
        sync_client.close()
        asyncio.run(async_client.aclose())


def test_close_httpx2_clients():
    async_client = MagicMock()
    async_client.aclose = AsyncMock()
    sync_client = MagicMock()
    _state.httpx2_async_client = async_client
    _state.httpx2_sync_client = sync_client

    http_mod._close_httpx2_clients()

    async_client.aclose.assert_awaited_once()
    sync_client.close.assert_called_once()
    assert _state.httpx2_async_client is None
    assert _state.httpx2_sync_client is None


async def test_close_httpx2_clients_with_running_loop():
    import asyncio

    async_client = MagicMock()
    async_client.aclose = AsyncMock()
    sync_client = MagicMock()
    _state.httpx2_async_client = async_client
    _state.httpx2_sync_client = sync_client

    http_mod._close_httpx2_clients()
    await asyncio.sleep(0)  # let the scheduled aclose task run

    sync_client.close.assert_called_once()
    assert _state.httpx2_async_client is None
    assert _state.httpx2_sync_client is None


# --- legacy httpx hooks + (un)instrument ------------------------------------


def test_legacy_httpx_hooks_set_attributes():
    rec = MagicMock()
    http_mod._httpx_request_hook(rec, FakeRequest())
    http_mod._httpx_response_hook(rec, FakeRequest(), FakeResponse(200))
    assert rec.set_attribute.called


async def test_legacy_httpx_async_hooks_set_attributes():
    rec = MagicMock()
    await http_mod._httpx_async_request_hook(rec, FakeRequest())
    await http_mod._httpx_async_response_hook(rec, FakeRequest(), FakeResponse(200))
    assert rec.set_attribute.called


def test_instrument_httpx_sets_flag_and_env(monkeypatch):
    pytest.importorskip("httpx2", reason="httpx2 ships with the enterprise OpenAI stack")
    import opentelemetry.instrumentation.httpx as otel_httpx

    instrumentor = MagicMock()
    monkeypatch.setattr(
        otel_httpx, "HTTPXClientInstrumentor", MagicMock(return_value=instrumentor)
    )
    http_mod._instrument_httpx()

    assert _state.httpx_instrumented is True  # httpx2 is installed
    assert os.environ.get("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST")
    instrumentor.instrument.assert_called_once()


def test_instrument_httpx_is_idempotent(monkeypatch):
    _state.httpx_instrumented = True
    import opentelemetry.instrumentation.httpx as otel_httpx

    instrumentor = MagicMock()
    monkeypatch.setattr(
        otel_httpx, "HTTPXClientInstrumentor", MagicMock(return_value=instrumentor)
    )
    http_mod._instrument_httpx()
    instrumentor.instrument.assert_not_called()


def test_uninstrument_httpx_resets_flag(monkeypatch):
    import opentelemetry.instrumentation.httpx as otel_httpx

    _state.httpx_instrumented = True
    instrumentor = MagicMock()
    monkeypatch.setattr(
        otel_httpx, "HTTPXClientInstrumentor", MagicMock(return_value=instrumentor)
    )
    http_mod._uninstrument_httpx()

    assert _state.httpx_instrumented is False
    instrumentor.uninstrument.assert_called_once()
