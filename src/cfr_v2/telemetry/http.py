from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Optional

from opentelemetry.trace import SpanKind, Status, StatusCode

from . import _state
from .api import get_logger, get_tracer

# Default HTTP header capture. Operators can override these through env vars.
_HTTPX_CAPTURE_ENV_DEFAULTS: dict[str, str] = {
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST": (
        "content-type,content-length,accept,user-agent"
    ),
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE": (
        "content-type,content-length,request-id,x-request-id,"
        "openai-processing-ms,x-ratelimit-.*"
    ),
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS": (
        "authorization,.*api.?key.*,.*token.*,.*secret.*,cookie,set-cookie"
    ),
}


def _httpx_header_size(headers: Any) -> Optional[int]:
    if headers is None:
        return None
    try:
        getter = getattr(headers, "get", None)
        raw = getter("content-length") if getter is not None else None
        if raw is None and getter is None:
            for key, value in headers:
                name = key.decode() if isinstance(key, bytes) else key
                if str(name).lower() == "content-length":
                    raw = value.decode() if isinstance(value, bytes) else value
                    break
        return int(raw) if raw is not None else None
    except Exception:  # pragma: no cover - defensive
        return None


def _split_header_patterns(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _header_matches(name: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, name, flags=re.IGNORECASE) for pattern in patterns)


def _capture_http_headers(
    span: Any,
    headers: Any,
    attribute_prefix: str,
    patterns: list[str],
    sanitize: list[str],
) -> None:
    if headers is None or not patterns:
        return
    try:
        items = headers.multi_items()
    except Exception:
        try:
            items = list(headers)
        except Exception:  # pragma: no cover - defensive
            return

    for name, value in items:
        if isinstance(name, bytes):
            name = name.decode("latin-1", errors="replace")
        name = str(name).lower()
        if not _header_matches(name, patterns):
            continue
        if _header_matches(name, sanitize):
            value = "[REDACTED]"
        elif isinstance(value, bytes):
            value = value.decode("latin-1", errors="replace")
        attr_name = f"{attribute_prefix}.{name.replace('-', '_')}"
        span.set_attribute(attr_name, str(value)[:2048])


def _set_httpx_request_attributes(span: Any, request: Any) -> None:
    try:
        method = getattr(request, "method", None)
        if method is not None:
            span.set_attribute("http.request.method", str(method))
        url = getattr(request, "url", None)
        if url is not None:
            host = getattr(url, "host", None)
            port = getattr(url, "port", None)
            scheme = getattr(url, "scheme", None)
            path = getattr(url, "path", None)
            if host:
                span.set_attribute("server.address", str(host))
            if port:
                span.set_attribute("server.port", int(port))
            if scheme:
                span.set_attribute("url.scheme", str(scheme))
            if path:
                span.set_attribute("url.path", str(path))
        headers = getattr(request, "headers", None)
        size = _httpx_header_size(headers)
        if size is not None:
            span.set_attribute("http.request.body.size", size)
        _capture_http_headers(
            span,
            headers,
            "http.request.header",
            _split_header_patterns(
                os.getenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST")
            ),
            _split_header_patterns(
                os.getenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS")
            ),
        )
    except Exception:  # pragma: no cover - defensive
        pass


def _set_httpx_response_attributes(span: Any, response: Any) -> None:
    try:
        status = getattr(response, "status_code", None)
        if status is not None:
            span.set_attribute("http.response.status_code", int(status))
            if int(status) >= 400:
                span.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))
        headers = getattr(response, "headers", None)
        size = _httpx_header_size(headers)
        if size is not None:
            span.set_attribute("http.response.body.size", size)
        _capture_http_headers(
            span,
            headers,
            "http.response.header",
            _split_header_patterns(
                os.getenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE")
            ),
            _split_header_patterns(
                os.getenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS")
            ),
        )
    except Exception:  # pragma: no cover - defensive
        pass


def _start_httpx2_span(request: Any) -> tuple[Any, float] | None:
    if not _state.enabled:
        return None
    try:
        tracer = get_tracer("httpx2.client")
        current_span = tracer.start_span(
            f"HTTP {getattr(request, 'method', 'REQUEST')}",
            kind=SpanKind.CLIENT,
        )
        _set_httpx_request_attributes(current_span, request)
        return current_span, time.perf_counter()
    except Exception:  # pragma: no cover - defensive
        return None


def _finish_httpx2_span(
    current_span: Any,
    start: float,
    response: Any = None,
    error: BaseException | None = None,
) -> None:
    try:
        if response is not None:
            _set_httpx_response_attributes(current_span, response)
        if error is not None:
            current_span.record_exception(error)
            current_span.set_attribute("error.type", type(error).__name__)
            current_span.set_status(Status(StatusCode.ERROR, str(error)[:500]))
        current_span.set_attribute(
            "http.duration_ms", (time.perf_counter() - start) * 1000.0
        )
    except Exception:  # pragma: no cover - defensive
        pass
    finally:
        try:
            current_span.end()
        except Exception:  # pragma: no cover - defensive
            pass


class InstrumentedHttpx2AsyncClientMixin:
    async def send(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        state = _start_httpx2_span(request)
        try:
            response = await super().send(request, *args, **kwargs)  # type: ignore[misc]
            if state is not None:
                _finish_httpx2_span(state[0], state[1], response=response)
            return response
        except BaseException as exc:
            if state is not None:
                _finish_httpx2_span(state[0], state[1], error=exc)
            raise


class InstrumentedHttpx2SyncClientMixin:
    def send(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        state = _start_httpx2_span(request)
        try:
            response = super().send(request, *args, **kwargs)  # type: ignore[misc]
            if state is not None:
                _finish_httpx2_span(state[0], state[1], response=response)
            return response
        except BaseException as exc:
            if state is not None:
                _finish_httpx2_span(state[0], state[1], error=exc)
            raise


def _make_httpx2_clients() -> tuple[Any, Any]:
    import httpx2
    from openai._constants import DEFAULT_CONNECTION_LIMITS, DEFAULT_TIMEOUT

    class InstrumentedAsyncClient(InstrumentedHttpx2AsyncClientMixin, httpx2.AsyncClient):
        pass

    class InstrumentedSyncClient(InstrumentedHttpx2SyncClientMixin, httpx2.Client):
        pass

    return (
        InstrumentedAsyncClient(
            timeout=DEFAULT_TIMEOUT,
            limits=DEFAULT_CONNECTION_LIMITS,
            follow_redirects=True,
        ),
        InstrumentedSyncClient(
            timeout=DEFAULT_TIMEOUT,
            limits=DEFAULT_CONNECTION_LIMITS,
            follow_redirects=True,
        ),
    )


def get_instrumented_httpx2_async_client() -> Any:
    if _state.httpx2_async_client is not None:
        return _state.httpx2_async_client
    with _state.httpx2_clients_lock:
        if _state.httpx2_async_client is None:
            _state.httpx2_async_client, _state.httpx2_sync_client = _make_httpx2_clients()
    return _state.httpx2_async_client


def get_instrumented_httpx2_sync_client() -> Any:
    if _state.httpx2_sync_client is not None:
        return _state.httpx2_sync_client
    with _state.httpx2_clients_lock:
        if _state.httpx2_sync_client is None:
            _state.httpx2_async_client, _state.httpx2_sync_client = _make_httpx2_clients()
    return _state.httpx2_sync_client


def safechain_httpx_kwargs() -> dict[str, Any]:
    """Return both custom clients required to instrument SafeChain/OpenAI calls."""
    return {
        "http_client": get_instrumented_httpx2_sync_client(),
        "http_async_client": get_instrumented_httpx2_async_client(),
    }


def _close_httpx2_clients() -> None:
    async_client, sync_client = _state.httpx2_async_client, _state.httpx2_sync_client
    _state.httpx2_async_client = None
    _state.httpx2_sync_client = None

    if async_client is not None:
        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is None:
                asyncio.run(async_client.aclose())
            else:
                running_loop.create_task(async_client.aclose())
        except Exception:  # pragma: no cover - defensive
            pass
    if sync_client is not None:
        try:
            sync_client.close()
        except Exception:  # pragma: no cover - defensive
            pass


# Legacy httpx instrumentation remains useful for other dependencies.
def _httpx_request_hook(span: Any, request: Any) -> None:
    _set_httpx_request_attributes(span, request)


def _httpx_response_hook(span: Any, request: Any, response: Any) -> None:
    _set_httpx_response_attributes(span, response)


async def _httpx_async_request_hook(span: Any, request: Any) -> None:
    _httpx_request_hook(span, request)


async def _httpx_async_response_hook(span: Any, request: Any, response: Any) -> None:
    _httpx_response_hook(span, request, response)


def _instrument_httpx() -> None:
    """Instrument legacy httpx and provide instrumented httpx2 clients."""
    if _state.httpx_instrumented:
        return

    for key, value in _HTTPX_CAPTURE_ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument(
            request_hook=_httpx_request_hook,
            response_hook=_httpx_response_hook,
            async_request_hook=_httpx_async_request_hook,
            async_response_hook=_httpx_async_response_hook,
        )
        get_logger("cfr_v2.telemetry").info(
            "Legacy httpx OpenTelemetry instrumentation enabled"
        )
    except ImportError:  # pragma: no cover - environment-dependent
        get_logger("cfr_v2.telemetry").info(
            "Legacy httpx OpenTelemetry instrumentation package not installed; "
            "continuing with custom httpx2 instrumentation."
        )
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - defensive
        get_logger("cfr_v2.telemetry").warning(
            "Could not instrument legacy httpx: %s", exc
        )

    try:
        import httpx2  # noqa: F401

        _state.httpx_instrumented = True
        get_logger("cfr_v2.telemetry").info(
            "httpx2 OpenTelemetry client instrumentation enabled"
        )
    except ImportError:  # pragma: no cover - environment-dependent
        get_logger("cfr_v2.telemetry").warning(
            "httpx2 is not installed; OpenAI 3.x HTTP spans will not be available."
        )


def _uninstrument_httpx() -> None:
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().uninstrument()
    except Exception:  # pragma: no cover - defensive
        pass
    _state.httpx_instrumented = False
