from __future__ import annotations

import functools
import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional

from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

from . import _state


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


@contextmanager
def span(name: str, **attributes: Any):
    tracer = get_tracer("cfr.pipeline")
    with tracer.start_as_current_span(name) as current_span:
        for key, value in attributes.items():
            current_span.set_attribute(key, value)
        try:
            yield current_span
        except Exception as exc:
            current_span.record_exception(exc)
            current_span.set_status(Status(StatusCode.ERROR, str(exc)[:500]))
            raise


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
