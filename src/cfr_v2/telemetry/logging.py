from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import _state
from .api import _current_span_context, get_logger


class JsonTraceHandler(logging.Handler):
    """Write JSONL log records with active OTel trace context."""

    def __init__(self, filename: str | os.PathLike[str]):
        super().__init__()
        self.filename = Path(filename)
        self.filename.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        span_ctx = _current_span_context()
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }

        if span_ctx is not None:
            trace_id, span_id = span_ctx
            log_entry["trace_id"] = trace_id
            log_entry["span_id"] = span_id

        if hasattr(record, "duration"):
            log_entry["wait_time_seconds"] = record.duration
        if hasattr(record, "run_id"):
            log_entry["run_id"] = str(record.run_id)

        try:
            with self.filename.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except OSError:  # pragma: no cover - logging must never break the pipeline
            pass


class _TraceFileFormatter(logging.Formatter):
    """Readable application-log formatter that appends trace/span IDs."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        suffix = ""
        span_ctx = _current_span_context()
        if span_ctx is not None:
            trace_id, span_id = span_ctx
            suffix = f" [trace={trace_id[:16]} span={span_id}]"
        return (
            f"{timestamp} {record.levelname:<8} "
            f"{record.name} - {record.getMessage()}{suffix}"
        )


def _remove_console_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        # FileHandler inherits StreamHandler, so use the exact type to avoid
        # removing our file handlers while removing ee-logging's console handler.
        if type(handler) is logging.StreamHandler:
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - defensive
                pass


def configure_file_logger(
    name: str,
    filename: str | os.PathLike[str],
    *,
    level: int | str | None = None,
    remove_console: bool = True,
) -> logging.Logger:
    """Route a named application logger to a file and keep OTel logging wired.

    This is safe to call before or after ``init_telemetry()``. If called before
    telemetry is initialised, the logger name/path are registered and the OTel
    handler is attached automatically when telemetry starts.
    """
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    _state.file_logger_configs[name] = (path, level)

    logger = get_logger(name, level=level)
    if remove_console:
        _remove_console_handlers(logger)

    resolved_level = logger.level if logger.level != logging.NOTSET else logging.INFO
    file_handler = next(
        (
            handler
            for handler in logger.handlers
            if isinstance(handler, logging.FileHandler)
            and os.path.abspath(handler.baseFilename) == os.path.abspath(str(path))
        ),
        None,
    )
    if file_handler is None:
        file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
        file_handler.setLevel(resolved_level)
        file_handler.setFormatter(_TraceFileFormatter())
        logger.addHandler(file_handler)
    else:
        file_handler.setLevel(resolved_level)

    logger.propagate = False
    return logger


def configure_httpx_logging(filename: str | os.PathLike[str]) -> None:
    """Send HTTP transport debug logs to the pipeline JSONL file, not stderr."""
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    for name in ("httpx", "httpx2"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        _remove_console_handlers(logger)
        existing = next(
            (
                handler
                for handler in logger.handlers
                if isinstance(handler, JsonTraceHandler)
                and os.path.abspath(str(handler.filename)) == os.path.abspath(str(path))
            ),
            None,
        )
        if existing is None:
            logger.addHandler(JsonTraceHandler(path))
        logger.propagate = False


def _rewire_registered_file_loggers() -> None:
    for name, (path, level) in list(_state.file_logger_configs.items()):
        configure_file_logger(name, path, level=level, remove_console=True)


def _detach_otel_log_handlers() -> None:
    loggers = [logging.getLogger()]
    manager_dict = getattr(logging.Logger.manager, "loggerDict", {})
    for existing in manager_dict.values():
        if isinstance(existing, logging.Logger):
            loggers.append(existing)
    for logger in loggers:
        for handler in list(getattr(logger, "handlers", [])):
            if type(handler).__module__.startswith("opentelemetry"):
                try:
                    logger.removeHandler(handler)
                    handler.close()
                except Exception:  # pragma: no cover - defensive
                    pass
