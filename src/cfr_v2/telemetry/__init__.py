"""Application telemetry facade over the ee-logging / OpenTelemetry stack.

This package preserves the public API previously exposed by ``cfr_v2/telemetry.py``.
Call sites import names directly from ``cfr_v2.telemetry``; the implementation is
split into focused submodules (``bootstrap``, ``logging``, ``http``, ``api``).
"""
from __future__ import annotations

from .api import (
    add_event,
    aobserve,
    attach,
    context_from_traceparent,
    current_context,
    current_trace_id,
    current_traceparent,
    detach,
    elf_headers,
    get_logger,
    get_meter,
    get_tracer,
    instruments,
    is_enabled,
    link_to_current,
    mark_error,
    record_llm_call,
    set_attributes,
    span,
    telemetry,
)
from .attributes import METRIC_LABELS, SPAN_ATTRIBUTES
from .bootstrap import init_telemetry, shutdown_telemetry
from .http import (
    get_instrumented_httpx2_async_client,
    get_instrumented_httpx2_sync_client,
    safechain_httpx_kwargs,
)
from .logging import (
    JsonTraceHandler,
    configure_file_logger,
    configure_httpx_logging,
)

__all__ = [
    "METRIC_LABELS",
    "SPAN_ATTRIBUTES",
    "JsonTraceHandler",
    "add_event",
    "aobserve",
    "attach",
    "configure_file_logger",
    "configure_httpx_logging",
    "context_from_traceparent",
    "current_context",
    "current_trace_id",
    "current_traceparent",
    "detach",
    "elf_headers",
    "get_instrumented_httpx2_async_client",
    "get_instrumented_httpx2_sync_client",
    "get_logger",
    "get_meter",
    "get_tracer",
    "init_telemetry",
    "instruments",
    "is_enabled",
    "link_to_current",
    "mark_error",
    "record_llm_call",
    "safechain_httpx_kwargs",
    "set_attributes",
    "shutdown_telemetry",
    "span",
    "telemetry",
]
