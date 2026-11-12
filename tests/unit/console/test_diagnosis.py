"""Why a job failed (LDR-0025 step 1): the reason read from the end of its log, and the fix."""

from __future__ import annotations

import pytest

from cfr_v2.console.diagnosis import diagnose, failure_reason

BUILD_FAILURE = """12:30:55 INFO     cfr_v2.telemetry - Telemetry initialised (service=cfr-complaints)
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "/x/src/cfr_v2/procedures/lightrag_engine.py", line 516, in build_lightrag
    raise StoreMismatchError(
cfr_v2.procedures.lightrag_engine.StoreMismatchError: lightrag_store was built from sha256:6a0c: build sha256:b724 in a new directory
"""


@pytest.mark.parametrize(
    ("log", "reason"),
    [
        (
            BUILD_FAILURE,
            "StoreMismatchError: lightrag_store was built from sha256:6a0c: build sha256:b724 in a new directory",
        ),
        (
            "store: x\nerror: no LightRAG store at lightrag_store_b72453c4ef4c: run lightrag-build\n\n",
            "no LightRAG store at lightrag_store_b72453c4ef4c: run lightrag-build",
        ),
        (
            "usage: python -m cfr_v2.routing\npython -m cfr_v2.routing: error: the following arguments are required: --verticals\n",
            "the following arguments are required: --verticals",
        ),
        (
            "09:00:01 ERROR    cfr_v2.runtime - worker: not starting: 14 jobs queued, more than 10\n",
            "worker: not starting: 14 jobs queued, more than 10",
        ),
        ("....F\n==== 1 failed, 600 passed in 40.1s ====\n", "1 failed, 600 passed in 40.1s"),
        ("KeyboardInterrupt\n", "KeyboardInterrupt"),
        ("all fine\nexit\n", None),
        ("", None),
    ],
)
def test_the_reason_is_the_last_line_that_says_what_failed(tmp_path, log, reason):
    path = tmp_path / "log.txt"
    path.write_text(log)
    assert failure_reason(path) == reason


def test_a_long_reason_is_shortened_and_a_missing_log_has_none(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("ValueError: " + "x" * 1000 + "\n")
    reason = failure_reason(path)
    assert reason is not None and len(reason) == 400 and reason.endswith("…")
    assert failure_reason(tmp_path / "missing.txt") is None


@pytest.mark.parametrize(
    ("reason", "href"),
    [
        (
            "StoreMismatchError: lightrag_store was built from sha256:6a0c: build sha256:b724 in a new directory",
            "/procedures#procedures.build",
        ),
        ("no LightRAG store at lightrag_store_b72453c4ef4c: run lightrag-build", "/procedures#procedures.build"),
        ("OperationalError: connection failed", None),
        ('UndefinedTable: relation "artifact" does not exist', "/system#system.migrate"),
        ("worker: not starting: 14 jobs queued, more than 10", "/system#queue-h"),
        ("RoutingSetupError: release cfr-v2-phase-d@a pins no routing_verticals or procedure_snapshot", "/releases"),
        ("HTTPStatusError: Client error '401 Unauthorized'", "/system#system.smoke"),
    ],
)
def test_known_failures_come_with_a_fix(reason, href):
    found = diagnose(reason)
    assert found is not None and found.href == href and found.explanation


def test_unknown_and_missing_reasons_have_no_diagnosis():
    assert diagnose("ZeroDivisionError: division by zero") is None and diagnose(None) is None
