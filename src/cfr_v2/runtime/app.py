"""Runtime lifecycle and case submission (P6, LDR-0002)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from dbos import (
    DBOS,
    DBOSConfig,
    Queue,
    SetWorkflowID,
    SQLAlchemyDatasource,
    WorkflowHandle,
)

from cfr_v2.artifacts import ArtifactStore, StoredArtifact, register
from cfr_v2.contracts import (
    AdmissionKey,
    AdmissionRecord,
    AdmissionStatus,
    Classification,
    IngestionRequest,
    Lane,
)
from cfr_v2.state import CaseStateRepository, make_engine, make_sessionmaker
from cfr_v2.telemetry import get_logger

from . import context
from .settings import RuntimeSettings
from .stages import StageHandler, default_handlers
from .workflow import case_intake_workflow

logger = get_logger(__name__)

QUEUE_NAMES = {Lane.BATCH: "cfr-batch", Lane.REALTIME: "cfr-realtime"}


def start_runtime(
    settings: RuntimeSettings,
    store: ArtifactStore,
    *,
    handlers: Mapping[str, StageHandler] | None = None,
    listen_lanes: Sequence[Lane] | None = None,
) -> context.Runtime:
    """Configure DBOS, launch it (recovering this executor's pending workflows) and register lanes.

    ``listen_lanes`` limits which lanes this process executes; ``()`` makes a
    submit-only process (e.g. an intake service). ``None`` serves every lane.
    """
    slots = settings.workflow_slots
    s3_pool = getattr(getattr(store, "settings", None), "max_pool_connections", None)
    if s3_pool is not None and s3_pool < slots:
        logger.warning("S3 connection pool (%d) is smaller than workflow slots (%d); workflows will queue on S3", s3_pool, slots)
    config: DBOSConfig = {
        "name": settings.app_name,
        # Each running workflow checkpoints through the system DB; DBOS defaults to 20.
        "sys_db_pool_size": slots + settings.db_pool_headroom,
        "system_database_url": settings.dbos_system_database_url,
        "application_version": settings.app_version,
        "executor_id": settings.executor_id,
        "notification_listener_polling_interval_sec": min(1.0, settings.queue_polling_interval_sec),
    }
    DBOS(config=config)
    ds = SQLAlchemyDatasource.create(
        settings.state_database_url,
        # No pre-ping on the hot path (one extra round trip per checkout); recycle instead.
        engine_kwargs={
            "pool_size": slots,
            "max_overflow": settings.db_pool_headroom,
            "pool_pre_ping": False,
            "pool_recycle": settings.db_pool_recycle_sec,
        },
        schema=settings.tx_checkpoint_schema,
    )
    runtime = context.Runtime(
        settings=settings,
        ds=ds,
        store=store,
        handlers=handlers or default_handlers(),
        sessions=make_sessionmaker(
            make_engine(settings.state_database_url, pool_pre_ping=False, pool_recycle=settings.db_pool_recycle_sec)
        ),
    )
    # Installed before launch: recovery may resume workflows immediately.
    context.install(runtime)
    if listen_lanes is not None:
        DBOS.listen_queues([QUEUE_NAMES[lane] for lane in listen_lanes])
    DBOS.launch()
    for lane, concurrency in (
        (Lane.BATCH, settings.batch_queue_concurrency),
        (Lane.REALTIME, settings.realtime_queue_concurrency),
    ):
        runtime.queues[lane] = DBOS.register_queue(
            QUEUE_NAMES[lane],
            worker_concurrency=concurrency,
            polling_interval_sec=settings.queue_polling_interval_sec,
            on_conflict="always_update",
        )
    logger.info("runtime started app=%s version=%s executor=%s", settings.app_name, settings.app_version, settings.executor_id)
    return runtime


def stop_runtime(*, workflow_completion_timeout_sec: int = 0) -> None:
    DBOS.destroy(workflow_completion_timeout_sec=workflow_completion_timeout_sec)
    context.install(None)


def queue_for(lane: Lane) -> Queue:
    queue = context.current().queues.get(lane)
    if queue is None:
        raise RuntimeError(f"queue for lane {lane} not registered")
    return queue


def submit(
    request: IngestionRequest,
    *,
    release_manifest_ref: str,
    artifacts: Sequence[StoredArtifact] = (),
    admitted_detail: dict | None = None,
) -> tuple[AdmissionRecord, WorkflowHandle[str] | None]:
    """Admit a request and enqueue its workflow on the request's lane.

    ``artifacts`` (already uploaded, e.g. by C01) are registered in the same
    transaction as the admission, so a committed admission never references
    unregistered content.

    Safe to repeat: a duplicate admission re-enqueues with the same workflow id,
    which DBOS resolves to the existing workflow. This also heals a crash between
    the admission commit and the enqueue.
    """
    rt = context.current()
    stored = rt.store.put(
        request.model_dump_json().encode(), media_type="application/json", classification=Classification.RESTRICTED
    )
    with rt.sessions.begin() as session:
        repo = CaseStateRepository(session)
        for artifact in (*artifacts, stored):
            register(repo, artifact)
        record = repo.admit(
            request,
            request_ref=stored.metadata.ref,
            release_manifest_ref=release_manifest_ref,
            admitted_detail=admitted_detail,
        )
    if record.status is AdmissionStatus.REJECTED or record.case_run_id is None:
        return record, None
    with SetWorkflowID(record.case_run_id):
        handle = queue_for(request.lane).enqueue(case_intake_workflow, record.case_run_id)
    return record, handle


def reject(key: AdmissionKey, *, reason: str, request_hash: str) -> AdmissionRecord:
    """Record a rejected admission; no run is created and nothing is enqueued."""
    with context.current().sessions.begin() as session:
        return CaseStateRepository(session).reject(key, reason=reason, request_hash=request_hash)
