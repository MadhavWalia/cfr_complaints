"""Exception logging without messages (LDR-0012 Part 2): types and frames stay, content goes."""

from __future__ import annotations

import logging

from cfr_v2.telemetry import SafeExceptionFilter, get_logger, safe_traceback

SECRET = "customer said: my card ending 4242 was charged twice"
WRAPPED = "model reply: " + SECRET  # messages come from variables: source lines in frames are code, not data


def _raise_chain():
    try:
        raise ValueError(SECRET)
    except ValueError as exc:
        raise RuntimeError(WRAPPED) from exc


def _caught():
    try:
        _raise_chain()
    except RuntimeError as exc:
        return exc
    raise AssertionError("unreachable")


def test_safe_traceback_keeps_types_and_frames_but_no_messages():
    text = safe_traceback(_caught())
    assert SECRET not in text and "model reply" not in text
    assert "builtins.ValueError (message withheld)" in text
    assert "builtins.RuntimeError (message withheld)" in text
    assert "direct cause" in text
    assert "_raise_chain" in text  # frames are kept for debugging
    assert text.index("ValueError") < text.index("RuntimeError")  # cause first, as Python prints it


def test_implicit_context_is_kept_and_suppressed_context_is_not():
    try:
        try:
            raise KeyError(SECRET)
        except KeyError:
            raise TypeError("second") from None
    except TypeError as exc:
        suppressed = safe_traceback(exc)
    assert "KeyError" not in suppressed and SECRET not in suppressed

    try:
        try:
            raise KeyError(SECRET)
        except KeyError:
            raise TypeError("second")  # implicit context, on purpose
    except TypeError as exc:
        implicit = safe_traceback(exc)
    assert "builtins.KeyError (message withheld)" in implicit and "another exception occurred" in implicit
    assert SECRET not in implicit


def test_loggers_from_get_logger_strip_exception_messages(caplog):
    logger = get_logger("cfr_v2.tests.safe_exceptions")
    assert sum(isinstance(f, SafeExceptionFilter) for f in logger.filters) == 1
    get_logger("cfr_v2.tests.safe_exceptions")  # asking again adds no second filter
    assert sum(isinstance(f, SafeExceptionFilter) for f in logger.filters) == 1

    caplog.set_level(logging.ERROR, logger="cfr_v2.tests.safe_exceptions")
    try:
        _raise_chain()
    except RuntimeError:
        logger.exception("stage failed (%s)", "RuntimeError")
    [record] = caplog.records
    assert record.exc_info is None
    formatted = logging.Formatter().format(record)
    assert "stage failed (RuntimeError)" in formatted
    assert "RuntimeError (message withheld)" in formatted
    assert SECRET not in formatted
