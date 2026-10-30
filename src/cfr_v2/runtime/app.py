"""Runtime lifecycle and case submission (P6, LDR-0011)."""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from cfr_v2.artifacts import ArtifactStore, StoredArtifact, register
from cfr_v2.contracts import AdmissionKey, AdmissionRecord, AdmissionStatus, Classification, IngestionRequest, Lane
from cfr_v2.state import CaseStateRepository, Database
from cfr_v2.telemetry import current_traceparent, get_logger, link_to_current, set_attributes, span

from . import context, jobs, metrics
from .runner import Runner
from .settings import RuntimeSettings
from .stages import FINAL_STATE, StageHandler, default_handlers

logger = get_logger(__name__)


class WorkflowFailedError(RuntimeError):
    """The case run ended FAILED; the message is the recorded error type."""


@dataclass(frozen=True)
class RunHandle:
    """Observe one case run's job from any process."""

    case_run_id: str
    db: Database

    def get_workflow_id(self) -> str:
        return self.case_run_id

    def status(self) -> str | None:
        with self.db.transaction() as tx:
            found = jobs.job_status(tx, self.case_run_id)
        return None if found is None else found[0]

    def get_result(self, *, timeout: float = 120.0, poll_sec: float = 0.05) -> str:
        """Block until the run finishes; ``FINAL_STATE`` or ``WorkflowFailedError``."""
        deadline = time.monotonic() + timeout
        while True:
            with self.db.transaction() as tx:
                found = jobs.job_status(tx, self.case_run_id)
            if found is not None and found[0] == "SUCCEEDED":
                return FINAL_STATE
            if found is not None and found[0] == "FAILED":
                raise WorkflowFailedError(found[1] or "FAILED")
            if time.monotonic() > deadline:
                raise TimeoutError(f"case run {self.case_run_id} not finished after {timeout}s")
            time.sleep(poll_sec)


def start_runtime(
    settings: RuntimeSettings,
    store: ArtifactStore,
    *,
    handlers: Mapping[str, StageHandler] | None = None,
    listen_lanes: Sequence[Lane] | None = None,
) -> context.Runtime:
    """Open the database pool and start this process's lane workers.

    ``listen_lanes`` limits which lanes this process executes; ``()`` makes a
    submit-only process (e.g. an intake service). ``None`` serves every lane.
    Jobs left by dead processes are picked up automatically once their leases expire.
    """
    lanes = list(Lane) if listen_lanes is None else list(listen_lanes)
    slots = sum(
        {Lane.BATCH: settings.batch_queue_concurrency, Lane.REALTIME: settings.realtime_queue_concurrency}[lane]
        for lane in lanes
    )
    s3_pool = getattr(getattr(store, "settings", None), "max_pool_connections", None)
    if s3_pool is not None and s3_pool < slots:
        logger.warning("S3 connection pool (%d) is smaller than workflow slots (%d); workflows will queue on S3", s3_pool, slots)
    db = Database(
        settings.state_database_url,
        max_connections=slots + settings.db_pool_headroom,
        recycle_sec=settings.db_pool_recycle_sec,
    )
    runtime = context.Runtime(settings=settings, db=db, store=store, handlers=handlers or default_handlers())
    context.install(runtime)
    runtime.runner = Runner(runtime, lanes)
    runtime.runner.start()
    if lanes:
        metrics.register_queue_depth()
    logger.info(
        "runtime started executor=%s lanes=%s driver=%s",
        settings.executor_id,
        ",".join(lane.value for lane in lanes) or "none",
        db.driver.name,
    )
    return runtime


def stop_runtime(*, workflow_completion_timeout_sec: float = 0) -> None:
    """Stop the lanes (waiting up to the timeout for running jobs) and close the pool.

    Jobs still running are handed back so another process can take them at once.
    """
    rt = context.current()
    if rt.runner is not None:
        rt.runner.stop(workflow_completion_timeout_sec)
    rt.db.close()
    context.install(None)


def submit(
    request: IngestionRequest,
    *,
    release_manifest_ref: str,
    artifacts: Sequence[StoredArtifact] = (),
    admitted_detail: dict | None = None,
) -> tuple[AdmissionRecord, RunHandle | None]:
    """Admit a request and queue its run on the request's lane, in one transaction.

    ``artifacts`` (already uploaded, e.g. by C01) are registered in the same
    transaction, so a committed admission never references unregistered content.
    Safe to repeat: a duplicate admission finds the existing job and queues nothing.

    Each case run gets its own trace (LDR-0012): a ``case_run.admit`` root span,
    linked to the caller's span (e.g. the intake batch), whose context is stored on
    the job so every process that runs the job continues the same trace.
    """
    rt = context.current()
    with span(
        "case_run.admit",
        new_trace=True,
        links=link_to_current(),
        case_no=request.key.source_case_id,
        source_system=request.key.source_system,
        lane=request.lane.value,
        manifest_ref=release_manifest_ref,
    ) as admit_span:
        stored = rt.store.put(
            request.model_dump_json().encode(), media_type="application/json", classification=Classification.RESTRICTED
        )
        with rt.db.transaction() as tx:
            repo = CaseStateRepository(tx)
            for artifact in (*artifacts, stored):
                register(repo, artifact)
            record = repo.admit(
                request,
                request_ref=stored.metadata.ref,
                release_manifest_ref=release_manifest_ref,
                admitted_detail=admitted_detail,
            )
            set_attributes(admit_span, case_run_id=record.case_run_id, admission_status=record.status.value)
            if record.status is AdmissionStatus.REJECTED or record.case_run_id is None:
                return record, None
            jobs.enqueue(
                tx,
                record.case_run_id,
                request.lane,
                max_attempts=rt.settings.max_job_attempts,
                trace_context=current_traceparent(),
            )
    if rt.runner is not None:
        rt.runner.wake(request.lane)
    return record, RunHandle(record.case_run_id, rt.db)


def reject(key: AdmissionKey, *, reason: str, request_hash: str) -> AdmissionRecord:
    """Record a rejected admission; no run is created and nothing is queued."""
    with context.current().db.transaction() as tx:
        return CaseStateRepository(tx).reject(key, reason=reason, request_hash=request_hash)
