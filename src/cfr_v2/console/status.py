"""What the home page reads from the state database: cases per stage, the queue by release, and
which workers hold leases. Read-only; ids and counts only."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from cfr_v2.contracts import ReleaseManifest
from cfr_v2.runtime.release_check import QUEUE_SQL, QueuedRelease, assess_queue

# The pipeline line, in order: (coarse state, label).
LINE = (
    ("ADMITTED", "Admitted"),
    ("UNDERSTANDING", "C02 understanding"),
    ("SELECTING", "C03 selection"),
    ("ROUTING", "C04 routing"),
    ("PLANNING", "C05 planning"),
    ("PLAN_READY", "Plan ready"),
)

STAGE_SQL = (
    "SELECT state, count(*) FROM ("
    " SELECT DISTINCT ON (case_run_id) case_run_id, state FROM case_run_event"
    " ORDER BY case_run_id, event_id DESC) latest GROUP BY state"
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
    total: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "total", sum(s.count for s in self.line) + self.failed)

    @property
    def queued(self) -> int:
        return sum(r.queued for r in self.releases)

    @property
    def running(self) -> int:
        return sum(r.running for r in self.releases)


def unavailable(reason: str) -> PipelineStatus:
    return PipelineStatus(tuple(Stop(s, label, 0) for s, label in LINE), 0, (), (), error=reason)


def read_status(db, resolve: Callable[[str], ReleaseManifest]) -> PipelineStatus:
    with db.transaction() as tx:
        by_state = {state: int(n) for state, n in tx.all(STAGE_SQL)}
        queue = [tuple(r) for r in tx.all(QUEUE_SQL)]
        workers = tuple((owner, int(n)) for owner, n in tx.all(WORKERS_SQL))
    return PipelineStatus(
        tuple(Stop(state, label, by_state.get(state, 0)) for state, label in LINE),
        by_state.get("FAILED", 0) + by_state.get("CANCELLED", 0),
        tuple(assess_queue(queue, resolve)),
        workers,
    )
