"""Runner metrics (LDR-0012).

Labels are bounded enums only: lane, stage, outcome, status. Case identifiers
belong on spans, never here. Instruments are created at import; before telemetry
starts they are OTel no-op proxies that bind to the real provider once it is set.
"""
from __future__ import annotations

import threading
from collections.abc import Iterable

from opentelemetry.metrics import CallbackOptions, Observation

from cfr_v2.telemetry import get_logger, get_meter

from . import context, jobs

logger = get_logger(__name__)
_meter = get_meter("cfr.runtime")

claim_wait = _meter.create_histogram(
    "cfr.queue.claim_wait", unit="s", description="Time a job waited between becoming due and being claimed"
)
claims = _meter.create_counter(
    "cfr.job.claims", description="Jobs claimed; outcome=reclaimed when taken over after an expired lease"
)
job_retries = _meter.create_counter("cfr.job.retries", description="Runs requeued after an infrastructure error")
job_finished = _meter.create_counter("cfr.job.finished", description="Runs finished, by outcome (succeeded, failed)")
stage_duration = _meter.create_histogram(
    "cfr.stage.duration", unit="s", description="Stage execution time, excluding stages skipped on resume"
)
stage_outcomes = _meter.create_counter(
    "cfr.stage.outcomes", description="Stage outcomes: committed, duplicate, conflict, skipped, failed"
)
produce_retries = _meter.create_counter("cfr.stage.produce_retries", description="Produce attempts retried")

_gauge_lock = threading.Lock()
_gauge_registered = False


def _observe_queue_depth(options: CallbackOptions) -> Iterable[Observation]:
    """Unfinished jobs per lane and status, reported by processes that run lanes."""
    try:
        rt = context.current()
    except RuntimeError:
        return []
    if rt.runner is None or not rt.runner.lanes:
        return []
    try:
        with rt.db.transaction() as tx:
            rows = jobs.queue_depth(tx)
    except Exception:  # metrics are best-effort; never break the export thread
        logger.warning("runtime metrics: queue depth query failed")
        return []
    found = {(lane, status): count for lane, status, count in rows}
    return [
        Observation(found.get((lane, status), 0), {"lane": lane, "status": status.lower()})
        for lane in ("batch", "realtime")
        for status in ("QUEUED", "RUNNING")
    ]


def register_queue_depth() -> None:
    """Create the queue-depth gauge once per process (called when lanes start)."""
    global _gauge_registered
    with _gauge_lock:
        if _gauge_registered:
            return
        _meter.create_observable_gauge(
            "cfr.queue.depth", callbacks=[_observe_queue_depth], description="Unfinished jobs by lane and status"
        )
        _gauge_registered = True
