"""Release compatibility checks (LDR-0022): before admitting cases, and before a worker starts.

A case run keeps the release manifest it was admitted under (ADR-14), and the worker runs the
stages in this code (``STAGES``). When a stage's version in the code moves on, runs admitted under
an older release no longer match: on 2026-11-10 a worker started for a three-case test also ran
about 8,600 queued runs of an older release through C02 and C03, and every one failed at the new
routing stage. These checks stop that before any model is called:

- **Intake** refuses to admit under a release whose stage versions differ from the code's.
- **A worker** lists what is queued, by release, and refuses to start while queued or running jobs
  belong to a release it can't run, or when more are queued than ``--max-queued`` allows.

Both can be overridden on purpose (``--allow-incompatible``). The messages name releases, stages,
versions and counts only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from cfr_v2.contracts import ReleaseManifest

from .stages import STAGES, Stage


@dataclass(frozen=True)
class Mismatch:
    stage: str
    code_version: str
    release_version: str | None  # None: the release doesn't name this stage

    def __str__(self) -> str:
        pinned = self.release_version or "nothing"
        return f"{self.stage}: code runs {self.code_version}, release pins {pinned}"


def stage_mismatches(manifest: ReleaseManifest, stages: Sequence[Stage] = STAGES) -> list[Mismatch]:
    """Stages whose version in the release differs from the version this code runs."""
    pinned = {p.program_id: p.version for p in manifest.programs}
    return [
        Mismatch(s.logical_task_id, s.program_version, pinned.get(s.logical_task_id))
        for s in stages
        if pinned.get(s.logical_task_id) != s.program_version
    ]


class IncompatibleReleaseError(RuntimeError):
    pass


def require_compatible(manifest: ReleaseManifest, stages: Sequence[Stage] = STAGES) -> None:
    mismatches = stage_mismatches(manifest, stages)
    if mismatches:
        raise IncompatibleReleaseError(
            f"release {manifest.ref} doesn't match this code: " + "; ".join(str(m) for m in mismatches)
        )


@dataclass(frozen=True)
class QueuedRelease:
    manifest_ref: str
    queued: int
    running: int
    mismatches: tuple[Mismatch, ...]
    unknown: bool = False  # the manifest was never pinned, so nothing says what it runs

    @property
    def compatible(self) -> bool:
        return not self.unknown and not self.mismatches


QUEUE_SQL = (
    "SELECT c.release_manifest_ref, j.status, count(*) FROM workflow_job j"
    " JOIN case_run c USING (case_run_id) WHERE j.status IN ('QUEUED', 'RUNNING') GROUP BY 1, 2"
)


def assess_queue(
    rows: Iterable[tuple[str, str, int]],
    resolve: Callable[[str], ReleaseManifest],
    stages: Sequence[Stage] = STAGES,
) -> list[QueuedRelease]:
    """``rows``: (manifest ref, job status, count) for queued and running jobs. ``resolve`` loads a
    pinned manifest and raises for one that was never pinned."""
    counts: dict[str, dict[str, int]] = {}
    for ref, status, count in rows:
        counts.setdefault(ref, {})[status] = int(count)
    found = []
    for ref, by_status in sorted(counts.items()):
        try:
            mismatches, unknown = tuple(stage_mismatches(resolve(ref), stages)), False
        except Exception:  # noqa: BLE001 - an unknown release is reported, not raised
            mismatches, unknown = (), True
        found.append(QueuedRelease(ref, by_status.get("QUEUED", 0), by_status.get("RUNNING", 0), mismatches, unknown))
    return found


def queue_problems(releases: Sequence[QueuedRelease]) -> list[str]:
    """Queued or running jobs this code can't run, by release."""
    problems = []
    for r in releases:
        jobs = f"{r.queued} queued and {r.running} running"
        if r.unknown:
            problems.append(f"{jobs} under {r.manifest_ref}, a release that was never pinned")
        elif r.mismatches:
            detail = "; ".join(str(m) for m in r.mismatches)
            problems.append(f"{jobs} under {r.manifest_ref}, which doesn't match this code ({detail})")
    return problems


def over_cap(releases: Sequence[QueuedRelease], max_queued: int | None) -> str | None:
    queued = sum(r.queued for r in releases)
    if max_queued is not None and queued > max_queued:
        return f"{queued} jobs queued, more than --max-queued {max_queued}"
    return None
