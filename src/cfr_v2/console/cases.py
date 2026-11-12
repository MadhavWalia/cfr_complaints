"""What the Cases and Releases pages read (LDR-0023 decisions 7 and 8). Read-only.

Stage results are read through an INTERNAL-only reader, and only refs a run's stages wrote: the
console never reads RESTRICTED artifacts (transcripts, source records, redaction maps), whatever
it is asked for. Each case links to its trace and logs in Grafana.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cfr_v2.contracts import AuthorizationContext, Classification, ReleaseManifest
from cfr_v2.procedures import stores
from cfr_v2.runtime.release_check import Mismatch, stage_mismatches
from cfr_v2.runtime.stages import STAGES

from .status import LINE

READER = AuthorizationContext(principal="console", purpose="show stage results")  # INTERNAL only
STATES = (*(state for state, _ in LINE), "FAILED", "CANCELLED")
STATE_LABELS = dict(LINE) | {"FAILED": "Failed", "CANCELLED": "Cancelled"}
STAGE_LABELS = {
    "c02.pass1": "C02 Pass 1",
    "c02.pass2": "C02 Pass 2",
    "c03.primary_selection": "C03 selection",
    "c04.routing": "C04 routing",
    "c05.planning": "C05 planning",
}
PAGE_ROWS = 50

_LATEST = (
    "SELECT DISTINCT ON (case_run_id) case_run_id, state, created_at FROM case_run_event"
    " ORDER BY case_run_id, event_id DESC"
)
_RUNS = (
    f"WITH latest AS ({_LATEST})"
    " SELECT r.case_run_id, c.source_case_id, rm.release_id, r.release_manifest_ref, r.lane, l.state,"
    " r.created_at, l.created_at"
    ' FROM case_run r JOIN "case" c ON c.case_id = r.case_id'
    " LEFT JOIN latest l ON l.case_run_id = r.case_run_id"
    " LEFT JOIN release_manifest rm ON rm.manifest_ref = r.release_manifest_ref"
)
CASES_SQL = (
    f"{_RUNS} WHERE (%(state)s::text IS NULL OR l.state = %(state)s)"
    " AND (%(release)s::text IS NULL OR rm.release_id = %(release)s)"
    " AND (%(q)s::text IS NULL OR c.source_case_id ILIKE %(like)s OR r.case_run_id = %(q)s)"
    " ORDER BY r.created_at DESC, r.case_run_id LIMIT %(limit)s OFFSET %(offset)s"
)
CASE_SQL = f"{_RUNS} WHERE r.case_run_id = %(run)s"
EVENTS_SQL = "SELECT state, created_at, detail FROM case_run_event WHERE case_run_id = %(run)s ORDER BY event_id"
TASKS_SQL = (
    "SELECT logical_task_id, program_version, status, error_code, output_artifact_refs, created_at"
    " FROM task_execution WHERE case_run_id = %(run)s ORDER BY created_at, logical_task_id"
)
JOB_SQL = (
    "SELECT status, attempts, max_attempts, last_error, lease_owner, trace_context, updated_at"
    " FROM workflow_job WHERE case_run_id = %(run)s"
)
CLASSIFICATIONS_SQL = "SELECT DISTINCT classification FROM artifact WHERE sha256 = %(sha)s"
RELEASES_SQL = (
    "SELECT rm.release_id, rm.manifest_ref, rm.created_at, count(r.case_run_id)"
    " FROM release_manifest rm LEFT JOIN case_run r ON r.release_manifest_ref = rm.manifest_ref"
    " GROUP BY 1, 2, 3 ORDER BY rm.created_at DESC"
)


@dataclass(frozen=True)
class CaseRow:
    case_run_id: str
    case_no: str
    release_id: str | None
    manifest_ref: str
    lane: str
    state: str | None
    admitted_at: datetime
    updated_at: datetime | None

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state or "", self.state or "Not started")

    @property
    def pill(self) -> str:
        return {"PLAN_READY": "done", "FAILED": "failed", "CANCELLED": "waiting", "ADMITTED": "waiting"}.get(
            self.state or "", "running"
        )


@dataclass(frozen=True)
class Event:
    state: str
    at: datetime
    detail: dict[str, Any]

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)


@dataclass(frozen=True)
class StageResult:
    task: str
    version: str
    status: str
    error_code: str | None
    refs: tuple[str, ...]
    at: datetime

    @property
    def label(self) -> str:
        return STAGE_LABELS.get(self.task, self.task)

    @property
    def pill(self) -> str:
        """In the console's one status vocabulary: running, done, failed, waiting."""
        return {"SUCCEEDED": "done", "FAILED_RETRYABLE": "failed", "FAILED_PERMANENT": "failed"}.get(
            self.status, "waiting"
        )

    @property
    def status_label(self) -> str:
        return {
            "SUCCEEDED": "done",
            "FAILED_RETRYABLE": "failed, to retry",
            "FAILED_PERMANENT": "failed",
            "CANCELLED": "cancelled",
        }.get(self.status, self.status.replace("_", " ").lower())


@dataclass(frozen=True)
class WorkflowJob:
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    lease_owner: str | None
    trace_id: str | None
    updated_at: datetime


@dataclass(frozen=True)
class CaseRun:
    row: CaseRow
    events: tuple[Event, ...]
    stages: tuple[StageResult, ...]
    job: WorkflowJob | None

    @property
    def refs(self) -> set[str]:
        """Every artifact its stages wrote: the only ones the console reads for this run."""
        return {ref for s in self.stages for ref in s.refs}


def _row(r: Sequence[Any]) -> CaseRow:
    return CaseRow(*r)


def list_cases(
    db, *, state: str | None = None, release: str | None = None, q: str | None = None, offset: int = 0
) -> list[CaseRow]:
    """A page of case runs, newest first. ``q`` matches a case number (part of it) or a run id."""
    q = (q or "").strip() or None
    params = {
        "state": state if state in STATES else None,
        "release": release or None,
        "q": q,
        "like": f"%{q}%" if q else None,
        "limit": PAGE_ROWS + 1,  # one more: is there a next page
        "offset": max(0, offset),
    }
    with db.transaction() as tx:
        return [_row(r) for r in tx.all(CASES_SQL, params)]


def trace_id(traceparent: str | None) -> str | None:
    """The trace id from a W3C traceparent, ``00-<32 hex>-<16 hex>-<flags>``."""
    parts = (traceparent or "").split("-")
    return parts[1] if len(parts) == 4 and len(parts[1]) == 32 else None


def read_case(db, case_run_id: str) -> CaseRun | None:
    with db.transaction() as tx:
        found = tx.one(CASE_SQL, {"run": case_run_id})
        if found is None:
            return None
        events = tuple(
            Event(s, at, d if isinstance(d, dict) else {}) for s, at, d in tx.all(EVENTS_SQL, {"run": case_run_id})
        )
        stages = tuple(
            StageResult(task, version, status, error, tuple(refs or ()), at)
            for task, version, status, error, refs, at in tx.all(TASKS_SQL, {"run": case_run_id})
        )
        job = tx.one(JOB_SQL, {"run": case_run_id})
    workflow = None
    if job is not None:
        status, attempts, max_attempts, last_error, owner, context, updated = job
        workflow = WorkflowJob(status, attempts, max_attempts, last_error, owner, trace_id(context), updated)
    return CaseRun(_row(found), events, stages, workflow)


@dataclass(frozen=True)
class Withheld:
    """A stage result the page doesn't show, and why."""

    reason: str


class NotShown(LookupError):
    """An artifact the console won't read: not this run's, or not INTERNAL."""


def read_result(db, store, run: CaseRun, ref: str) -> bytes:
    """A stage result of ``run``, INTERNAL only. Anything else raises ``NotShown``."""
    if ref not in run.refs or not ref.startswith("sha256:"):
        raise NotShown(ref)
    with db.transaction() as tx:
        stored = set(tx.column(CLASSIFICATIONS_SQL, {"sha": ref.removeprefix("sha256:")}))
    if Classification.INTERNAL.value not in stored:
        raise NotShown(ref)
    return store.get(ref, classification=Classification.INTERNAL, authorization=READER)


def parse_result(data: bytes) -> Any:
    try:
        return json.loads(data)
    except ValueError:
        return None


# --- Grafana (decision 8): links, never embedding ----------------------------------------------

TEMPO = {"type": "tempo", "uid": "tempo"}  # the provisioned datasources (docker/observability/grafana)
LOKI = {"type": "loki", "uid": "loki"}


def _explore(base: str, datasource: dict, query: dict, start: datetime, end: datetime) -> str:
    pane = {
        "datasource": datasource["uid"],
        "queries": [{"refId": "A", "datasource": datasource, **query}],
        "range": {"from": str(int(start.timestamp() * 1000)), "to": str(int(end.timestamp() * 1000))},
    }
    return f"{base.rstrip('/')}/explore?schemaVersion=1&orgId=1&panes={quote(json.dumps({'a': pane}, separators=(',', ':')))}"


def _window(run: CaseRun) -> tuple[datetime, datetime]:
    last = max([run.row.admitted_at, *(e.at for e in run.events)])
    return run.row.admitted_at - timedelta(minutes=10), last + timedelta(hours=1)


def trace_url(base: str, run: CaseRun) -> str | None:
    if run.job is None or run.job.trace_id is None:
        return None
    return _explore(base, TEMPO, {"queryType": "traceql", "query": run.job.trace_id}, *_window(run))


def logs_url(base: str, run: CaseRun) -> str | None:
    if run.job is None or run.job.trace_id is None:
        return None
    expr = f'{{service_name=~"cfr.*"}} | trace_id="{run.job.trace_id}"'
    return _explore(base, LOKI, {"expr": expr, "queryType": "range"}, *_window(run))


# --- releases ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pinned:
    manifest_ref: str
    pinned_at: datetime
    runs: int


@dataclass
class Release:
    release_id: str
    manifest: ReleaseManifest | None = None  # from registry/releases/<id>.yaml
    problem: str | None = None  # why the file couldn't be read
    mismatches: list[Mismatch] = field(default_factory=list)
    pinned: list[Pinned] = field(default_factory=list)  # every version pinned in the database, newest first
    blockers: list[str] = field(default_factory=list)  # why new cases can't be admitted under it

    @property
    def current_ref(self) -> str | None:
        return self.manifest.ref if self.manifest else None

    @property
    def runs(self) -> int:
        return sum(p.runs for p in self.pinned)


def blockers(release: Release, root: Path, env: Mapping[str, str]) -> list[str]:
    """Why new cases shouldn't be admitted under ``release``; empty when it can be made current.
    The same checks intake and the worker make, and the C04 stage's: a release that fails one
    would admit cases certain to fail."""
    if release.problem or release.manifest is None:
        return ["its file can't be read" if release.problem else "its file is no longer in the registry"]
    found = []
    if release.mismatches:
        found.append("its stage versions don't match this code, so intake refuses it")
    manifest = release.manifest
    if not manifest.routing_verticals or not manifest.procedure_snapshot:
        found.append("it pins no knowledge document or procedure snapshot, so routing would fail")
    elif stores.built_from(stores.store_dir(manifest.procedure_snapshot, root=root, env=env)) != (
        manifest.procedure_snapshot
    ):
        found.append("its procedure snapshot has no LightRAG store here: build it under Procedures")
    return found


def read_releases(registry: Path, db_rows: Callable[[], list[tuple]] | None) -> tuple[list[Release], str | None]:
    """Releases in the registry and the database. Returns them and why the database couldn't be read."""
    from cfr_v2.registry import ReleaseRegistry

    found: dict[str, Release] = {}
    reg = ReleaseRegistry(registry)
    for path in sorted((registry / "releases").glob("*.yaml")):
        release = found.setdefault(path.stem, Release(path.stem))
        try:
            release.manifest = reg.load(path.stem)
            release.mismatches = stage_mismatches(release.manifest, STAGES)
        except Exception as exc:  # noqa: BLE001 - shown on the page; a broken file mustn't hide the rest
            first = (str(exc).replace(str(registry), "registry").splitlines() or [type(exc).__name__])[0]
            release.problem = f"{path.name}: {first}"
    error = None
    if db_rows is not None:
        try:
            for release_id, ref, at, runs in db_rows():
                found.setdefault(release_id, Release(release_id)).pinned.append(Pinned(ref, at, int(runs)))
        except Exception as exc:  # noqa: BLE001 - the page shows the type; the message can name hosts
            error = type(exc).__name__
    return sorted(found.values(), key=lambda r: r.release_id, reverse=True), error


__all__ = [
    "PAGE_ROWS",
    "STAGES",
    "STATES",
    "CaseRow",
    "CaseRun",
    "NotShown",
    "Release",
    "Withheld",
    "blockers",
    "list_cases",
    "logs_url",
    "parse_result",
    "read_case",
    "read_releases",
    "read_result",
    "trace_url",
]
