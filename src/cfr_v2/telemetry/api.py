from __future__ import annotations

import functools
import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Callable, Optional

from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.trace import Link, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from . import _state
from .attributes import PREFIX, SPAN_ATTRIBUTES

_W3C = TraceContextTextMapPropagator()
_dropped_logged: set[str] = set()


def _current_span_context() -> tuple[str, str] | None:
    try:
        ctx = trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return None
        return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        return None


def telemetry() -> Any:
    return _state.telemetry


def is_enabled() -> bool:
    return _state.enabled


def get_logger(name: str, *, level: int | str | None = None) -> logging.Logger:
    if _state.enabled:
        from ee_logging import get_logger as _get_logger

        return _get_logger(name, level=level)
    return logging.getLogger(name)


def get_tracer(name: str) -> Any:
    # ee_logging sets the global TracerProvider; before init the OTel global API
    # returns a spec-compliant no-op tracer, so no local fallback is needed.
    return trace.get_tracer(name)


def get_meter(name: str) -> Any:
    # ee_logging sets the global MeterProvider; before init this is an OTel no-op.
    return metrics.get_meter(name)


def instruments() -> Any:
    return getattr(_state.telemetry, "instruments", None) if _state.enabled else None


def elf_headers(existing: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Return B3 trace-context headers for ELF correlation."""
    if not _state.enabled:
        return dict(existing or {})
    try:
        from ee_logging.tracing import get_elf_headers

        return get_elf_headers(dict(existing) if existing else None)
    except Exception:
        return dict(existing or {})


def _allowed(attributes: dict[str, Any]) -> dict[str, Any]:
    """Keep allowlisted, non-None attributes as ``cfr.<name>``; drop the rest (LDR-0012)."""
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if key not in SPAN_ATTRIBUTES:
            if key not in _dropped_logged:  # the key name only, never its value
                _dropped_logged.add(key)
                logging.getLogger("cfr_v2.telemetry").warning("span attribute %r is not allowlisted; dropped", key)
            continue
        if value is not None:
            out[PREFIX + key] = value
    return out


def set_attributes(current_span: Any, **attributes: Any) -> None:
    """Set allowlisted attributes on an open span (e.g. an outcome known at the end)."""
    current_span.set_attributes(_allowed(attributes))


def add_event(current_span: Any, name: str, **attributes: Any) -> None:
    current_span.add_event(name, _allowed(attributes))


def mark_error(current_span: Any, exc: BaseException) -> None:
    """Record a failure by exception type only.

    The message and traceback are left out: a message can quote its input (a
    validation error on a transcript, say), and spans must never carry case content.
    """
    error_type = type(exc).__name__
    current_span.add_event("exception", {"exception.type": f"{type(exc).__module__}.{error_type}"})
    current_span.set_status(Status(StatusCode.ERROR, error_type))


@contextmanager
def span(
    name: str,
    *,
    context: Any = None,
    links: Iterable[Link] = (),
    new_trace: bool = False,
    **attributes: Any,
) -> Iterator[Any]:
    """Open a span with allowlisted ``attributes``.

    ``context`` sets an explicit parent (e.g. restored from a job row);
    ``new_trace`` starts a fresh trace, typically with ``links`` back to the caller.
    """
    tracer = get_tracer("cfr.pipeline")
    parent = otel_context.Context() if new_trace else context
    with tracer.start_as_current_span(
        name,
        context=parent,
        links=list(links),
        attributes=_allowed(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as current_span:
        try:
            yield current_span
        except Exception as exc:
            mark_error(current_span, exc)
            raise


# --- trace context across processes ---------------------------------------------


def current_traceparent() -> str | None:
    """The active span as a W3C ``traceparent`` (stored on a job row), or None."""
    carrier: dict[str, str] = {}
    _W3C.inject(carrier)
    return carrier.get("traceparent")


def context_from_traceparent(traceparent: str | None) -> Any:
    """A parent context restored from ``traceparent``; None if absent or invalid."""
    if not traceparent:
        return None
    restored = _W3C.extract({"traceparent": traceparent})
    return restored if trace.get_current_span(restored).get_span_context().is_valid else None


def current_trace_id() -> str | None:
    found = _current_span_context()
    return None if found is None else found[0]


def link_to_current() -> list[Link]:
    """A link to the active span, if any (a case-run trace back to its batch)."""
    ctx = trace.get_current_span().get_span_context()
    return [Link(ctx)] if ctx.is_valid else []


def attach(ctx: Any) -> Any:
    """Make ``ctx`` current in this thread (worker pools don't inherit it); returns a token."""
    return otel_context.attach(ctx)


def detach(token: Any) -> None:
    otel_context.detach(token)


def current_context() -> Any:
    return otel_context.get_current()


def aobserve(func: Callable[..., Any]) -> Callable[..., Any]:
    cache: dict[str, Any] = {}

    @functools.wraps(func)
    async def _inner(*args: Any, **kwargs: Any) -> Any:
        if not _state.enabled:
            return await func(*args, **kwargs)
        decorated = cache.get("f")
        if decorated is None:
            try:
                from ee_logging import aobserve as _aobserve

                decorated = _aobserve(func)
            except Exception:
                decorated = func
            cache["f"] = decorated
        return await decorated(*args, **kwargs)

    return _inner


@contextmanager
def record_llm_call(model_id: str, operation: str = "completion", **extra: Any):
    inst = instruments()
    attrs = {"model": model_id, "operation": operation, **extra}
    start = time.perf_counter()
    try:
        yield
    except Exception:
        if inst is not None:
            try:
                inst.error_counter.add(1, attrs)
            except Exception:  # pragma: no cover - metrics are best-effort
                pass
        raise
    finally:
        if inst is not None:
            duration_ms = (time.perf_counter() - start) * 1000.0
            try:
                inst.request_counter.add(1, attrs)
                inst.request_duration.record(duration_ms, attrs)
            except Exception:  # pragma: no cover - metrics are best-effort
                pass
