from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

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
    """A logger whose exception records keep types and stack frames but no messages (``SafeExceptionFilter``)."""
    if _state.enabled:
        from ee_logging import get_logger as _get_logger

        logger = _get_logger(name, level=level)
    else:
        logger = logging.getLogger(name)
    if isinstance(logger, logging.Filterer) and not any(isinstance(f, SafeExceptionFilter) for f in logger.filters):
        logger.addFilter(SafeExceptionFilter())
    return logger


class SafeExceptionFilter(logging.Filter):
    """Strip exception messages from log records; keep the type chain and the stack frames.

    An exception's message can quote its input: a pydantic error on model output, a DSPy
    parse error quoting the reply, an SDK error echoing a request. ``logger.exception``
    would write it, with every chained cause, into logs that are not covered by the span
    allowlist (LDR-0012). This replaces ``exc_info`` with a traceback of frames (file,
    line, function, source line) and exception types only. As a logger filter it applies
    to records logged through that logger, which is how every ``cfr_v2`` module logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = safe_traceback(record.exc_info[1])
            record.exc_info = None
        return True


def safe_traceback(exc: BaseException) -> str:
    """Like ``traceback.format_exception``, without any exception message."""
    import traceback

    chain: list[tuple[BaseException, str]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    note = ""
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append((current, note))
        if current.__cause__ is not None:
            current, note = current.__cause__, "The above exception was the direct cause of the following exception:"
        elif current.__context__ is not None and not current.__suppress_context__:
            current, note = current.__context__, "During handling of the above exception, another exception occurred:"
        else:
            current = None
    parts: list[str] = []
    for i, (e, _) in enumerate(reversed(chain)):
        if i:
            parts.append(f"\n{chain[len(chain) - i][1]}\n\n")
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_tb(e.__traceback__))
        parts.append(f"{type(e).__module__}.{type(e).__qualname__} (message withheld)\n")
    return "".join(parts).rstrip("\n")


def get_tracer(name: str) -> Any:
    # ee_logging sets the global TracerProvider; before init the OTel global API
    # returns a spec-compliant no-op tracer, so no local fallback is needed.
    return trace.get_tracer(name)


def get_meter(name: str) -> Any:
    # ee_logging sets the global MeterProvider; before init this is an OTel no-op.
    return metrics.get_meter(name)


def instruments() -> Any:
    return getattr(_state.telemetry, "instruments", None) if _state.enabled else None


def elf_headers(existing: dict[str, str] | None = None) -> dict[str, str]:
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


# --- procedure index builds ---------------------------------------------------------
# Named here rather than taken from ee-logging's instruments, so a dashboard can rely on them.


STAGE_BUCKETS_SEC = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 240, 480, 960, 1800, 3600)


@functools.cache
def _build_instruments() -> tuple[Any, Any]:
    meter = get_meter("cfr.procedures")
    return (
        meter.create_histogram(
            "cfr.procedures.stage.duration",
            unit="s",
            description="Time per build stage",
            explicit_bucket_boundaries_advisory=STAGE_BUCKETS_SEC,
        ),
        meter.create_gauge("cfr.procedures.passages", description="Passages by status in the current build"),
    )


def record_stage(stage: str, seconds: float, outcome: str = "ok") -> None:
    """Time spent in one stage of a build. Best-effort, like every metric here."""
    try:
        _build_instruments()[0].record(seconds, {"stage": stage, "outcome": outcome})
    except Exception:  # pragma: no cover - metrics are best-effort
        pass


@functools.cache
def _retry_counter() -> Any:
    return get_meter("cfr.llm").create_counter(
        "cfr.llm.retries", description="Model and embedding attempts that failed and were tried again"
    )


def record_retry(stage: str, outcome: str, status: int | None = None) -> None:
    """One retried attempt: ``stage`` is chat or embedding, ``outcome`` the failure kind, ``status``
    the HTTP status if there was one. Counted here so retries show without a log pipeline."""
    try:
        _retry_counter().add(1, {"stage": stage, "outcome": outcome, "status": str(status or "none")})
    except Exception:  # pragma: no cover - metrics are best-effort
        pass


def record_passages(*, processed: int, failed: int, queued: int) -> None:
    try:
        gauge = _build_instruments()[1]
        for status, count in (("processed", processed), ("failed", failed), ("queued", queued)):
            gauge.set(count, {"status": status})
    except Exception:  # pragma: no cover - metrics are best-effort
        pass
