"""What the home page reads from the state database: cases per stage, the queue by release, and
which workers hold leases. Read-only; ids and counts only."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cfr_v2.contracts import ReleaseManifest
from cfr_v2.runtime.release_check import QUEUE_SQL, QueuedRelease, assess_queue

if TYPE_CHECKING:
    from .jobs import Job

# The pipeline line, in order: (coarse state, label).
LINE = (
    ("ADMITTED", "Admitted"),
    ("UNDERSTANDING", "C02 understanding"),
    ("SELECTING", "C03 selection"),
    ("ROUTING", "C04 routing"),
    ("PLANNING", "C05 planning"),
    ("PLAN_READY", "Plan ready"),
)

# Each run's latest state, under one release (the current one, by default: LDR-0029) or all.
STAGE_SQL = (
    "SELECT state, count(*) FROM ("
    " SELECT DISTINCT ON (e.case_run_id) e.case_run_id, e.state FROM case_run_event e"
    " JOIN case_run r USING (case_run_id)"
    " WHERE %(release)s::text IS NULL OR split_part(r.release_manifest_ref, '@', 1) = %(release)s"
    " ORDER BY e.case_run_id, e.event_id DESC) latest GROUP BY state"
)
WORKERS_SQL = (
    "SELECT lease_owner, count(*) FROM workflow_job"
    " WHERE status = 'RUNNING' AND lease_expires_at > now() GROUP BY 1 ORDER BY 1"
)


@dataclass(frozen=True)
class Stop:
    state: str
    label: str
    count: int


@dataclass(frozen=True)
class PipelineStatus:
    line: tuple[Stop, ...]
    failed: int
    releases: tuple[QueuedRelease, ...]
    workers: tuple[tuple[str, int], ...]  # (lease owner, jobs it holds)
    error: str | None = None  # why the database couldn't be read; nothing else is set then
    release: str | None = None  # the release the stage line and the failed count cover; None: all
    total: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "total", sum(s.count for s in self.line) + self.failed)

    @property
    def queued(self) -> int:
        return sum(r.queued for r in self.releases)

    @property
    def running(self) -> int:
        return sum(r.running for r in self.releases)


def unavailable(reason: str, release: str | None = None) -> PipelineStatus:
    return PipelineStatus(tuple(Stop(s, label, 0) for s, label in LINE), 0, (), (), error=reason, release=release)


def read_status(db, resolve: Callable[[str], ReleaseManifest], release: str | None = None) -> PipelineStatus:
    """The stage line and the failed count under ``release`` (None: every release). The queue and
    the workers are always across releases: a run queued under any release can stop a worker."""
    with db.transaction() as tx:
        by_state = {state: int(n) for state, n in tx.all(STAGE_SQL, {"release": release})}
        queue = [tuple(r) for r in tx.all(QUEUE_SQL)]
        workers = tuple((owner, int(n)) for owner, n in tx.all(WORKERS_SQL))
    return PipelineStatus(
        tuple(Stop(state, label, by_state.get(state, 0)) for state, label in LINE),
        by_state.get("FAILED", 0) + by_state.get("CANCELLED", 0),
        tuple(assess_queue(queue, resolve)),
        workers,
        release=release,
    )


# --- what needs attention: each exception with the one action that resolves it (LDR-0025) ----

RECENT_FAILURES = timedelta(hours=24)
MAX_FAILED_JOBS = 3


@dataclass(frozen=True)
class Attention:
    kind: str  # the pill: "failed", "blocked" (Caution) or "waiting"
    label: str  # the pill's text
    text: str  # one sentence, the count first when there is one
    action: str
    href: str


def _short(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def release_id(manifest_ref: str) -> str:
    """``cfr-v2-phase-e`` from ``cfr-v2-phase-e@6ff69ff5259b``."""
    return manifest_ref.split("@", 1)[0]


def attention(
    status: PipelineStatus, jobs: Sequence[Job], *, worker_job: Job | None, now: datetime | None = None
) -> list[Attention]:
    """Failed case runs, queued runs no worker may take, console jobs that failed in the last day,
    and runs waiting with no worker. Empty when nothing needs anyone."""
    if status.error:
        return []
    items: list[Attention] = []
    blocked = [r for r in status.releases if not r.compatible and r.queued]
    for r in blocked:
        why = "it was never pinned" if r.unknown else "it doesn't match this code"
        items.append(
            Attention(
                "blocked",
                "blocked",
                f"{r.queued:,} runs queued under {release_id(r.manifest_ref)} can't run: {why}. "
                "A worker won't start while they wait.",
                "Cancel them",
                f"/system?fill=system.cancel&release={release_id(r.manifest_ref)}#system.cancel",
            )
        )
    runnable = sum(r.queued for r in status.releases if r.compatible)
    if runnable and not status.workers and worker_job is None and not blocked:
        items.append(
            Attention(
                "waiting",
                "waiting",
                f"{runnable:,} runs are queued and no worker is running.",
                "Start a worker",
                "/system#system.worker",
            )
        )
    cutoff = (now or datetime.now(UTC)) - RECENT_FAILURES
    failed = [j for j in jobs if j.status.value == "failed" and datetime.fromisoformat(j.created_at) >= cutoff]
    for job in failed[:MAX_FAILED_JOBS]:
        reason = f": {_short(job.reason)}" if job.reason else ""
        items.append(Attention("failed", "failed", f"{job.title} failed{reason}", "See why", f"/jobs/{job.id}"))
    if len(failed) > MAX_FAILED_JOBS:
        more = len(failed) - MAX_FAILED_JOBS
        items.append(
            Attention("failed", "failed", f"{more} more jobs failed today.", "See them", "/jobs?status=failed")
        )
    if status.failed:
        under = f" under {status.release}" if status.release else ""
        items.append(
            Attention(
                "failed",
                "failed",
                f"{status.failed:,} case runs{under} failed or were cancelled.",
                "See them",
                f"/cases?state=FAILED&release={status.release or ''}",
            )
        )
    return items
