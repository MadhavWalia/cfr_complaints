from __future__ import annotations

import json
import logging

from opentelemetry import trace

from cfr_v2.telemetry import configure_file_logger, configure_httpx_logging
from cfr_v2.telemetry.logging import (
    JsonTraceHandler,
    _detach_otel_log_handlers,
    _TraceFileFormatter,
)


def test_configure_file_logger_writes_file(tmp_logs):
    path = tmp_logs / "app.log"
    logger = configure_file_logger("test.file.writer", path)
    # Disabled-path logger keeps the root WARNING effective level; log above it.
    logger.warning("hello world")
    for handler in logger.handlers:
        handler.flush()
    assert "hello world" in path.read_text()


def test_configure_file_logger_removes_console_handlers(tmp_logs):
    name = "test.file.console"
    logger = logging.getLogger(name)
    stream_handler = logging.StreamHandler()
    logger.addHandler(stream_handler)
    try:
        configure_file_logger(name, tmp_logs / "x.log", remove_console=True)
        assert stream_handler not in logger.handlers
    finally:
        logger.handlers.clear()


def test_configure_file_logger_reuses_existing_handler(tmp_logs):
    name = "test.file.reuse"
    path = tmp_logs / "reuse.log"
    try:
        configure_file_logger(name, path)
        configure_file_logger(name, path)
        logger = logging.getLogger(name)
        file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1
    finally:
        logging.getLogger(name).handlers.clear()


def test_trace_file_formatter_includes_trace_context(spans):
    formatter = _TraceFileFormatter()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("s"):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "hi", (), None)
        out = formatter.format(record)
    assert "[trace=" in out


def test_json_trace_handler_writes_jsonl(tmp_path):
    path = tmp_path / "trace.jsonl"
    handler = JsonTraceHandler(path)
    record = logging.LogRecord("mylog", logging.WARNING, __file__, 1, "msg %s", ("x",), None)
    handler.emit(record)

    data = json.loads(path.read_text().strip())
    assert data["message"] == "msg x"
    assert data["level"] == "WARNING"
    assert data["logger"] == "mylog"
    assert "trace_id" not in data  # no active span


def test_json_trace_handler_includes_trace_context(tmp_path, spans):
    path = tmp_path / "trace.jsonl"
    handler = JsonTraceHandler(path)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("s"):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
        handler.emit(record)

    data = json.loads(path.read_text().strip())
    assert "trace_id" in data
    assert "span_id" in data


def test_json_trace_handler_captures_extra_fields(tmp_path):
    path = tmp_path / "trace.jsonl"
    handler = JsonTraceHandler(path)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
    record.duration = 1.5
    record.run_id = 42
    handler.emit(record)

    data = json.loads(path.read_text().strip())
    assert data["wait_time_seconds"] == 1.5
    assert data["run_id"] == "42"


def test_configure_httpx_logging_attaches_handler(tmp_path):
    configure_httpx_logging(tmp_path / "httpx.jsonl")
    httpx_logger = logging.getLogger("httpx")
    try:
        assert any(isinstance(h, JsonTraceHandler) for h in httpx_logger.handlers)
        assert httpx_logger.propagate is False
    finally:
        logging.getLogger("httpx").handlers.clear()
        logging.getLogger("httpx2").handlers.clear()


def test_configure_httpx_logging_idempotent(tmp_path):
    configure_httpx_logging(tmp_path / "httpx.jsonl")
    configure_httpx_logging(tmp_path / "httpx.jsonl")
    httpx_logger = logging.getLogger("httpx")
    try:
        handlers = [h for h in httpx_logger.handlers if isinstance(h, JsonTraceHandler)]
        assert len(handlers) == 1
    finally:
        logging.getLogger("httpx").handlers.clear()
        logging.getLogger("httpx2").handlers.clear()


def test_detach_otel_log_handlers(tmp_path):
    class FakeOtelHandler(logging.Handler):
        def emit(self, record):  # pragma: no cover - never emitted
            pass

    FakeOtelHandler.__module__ = "opentelemetry.sdk._logs"

    logger = logging.getLogger("test.detach")
    otel_handler = FakeOtelHandler()
    keep_handler = logging.NullHandler()
    logger.addHandler(otel_handler)
    logger.addHandler(keep_handler)
    try:
        _detach_otel_log_handlers()
        assert otel_handler not in logger.handlers
        assert keep_handler in logger.handlers
    finally:
        logger.handlers.clear()
