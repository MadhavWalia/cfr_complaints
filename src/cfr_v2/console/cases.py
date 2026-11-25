"""What the Cases and Releases pages read (LDR-0023 decisions 7 and 8). Read-only.

Stage results are read through an INTERNAL-only reader, and only refs a run's stages wrote: the
console never reads RESTRICTED artifacts (transcripts, source records, redaction maps), whatever
it is asked for. The one other read is the baseline profile Pass 2 names in its result (the
customer accounts it was given, INTERNAL); the baseline's ref map is RESTRICTED and never read.
Each case links to its trace and logs in Grafana.

The list's filters (LDR-0037) are SQL where Postgres holds the fact (dates, the admission's
transcript and baseline, a failed stage) and the local fact index (``facts.py``) where only a
stage result does (claims, the primary claim, verticals, grounding).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta
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
_LAST_OK = (  # the latest successful result of a stage
    "(SELECT t.output_artifact_refs->>0 FROM task_execution t WHERE t.case_run_id = r.case_run_id"
    " AND t.logical_task_id = '{task}' AND t.status = 'SUCCEEDED' ORDER BY t.created_at DESC LIMIT 1)"
)
CASES_SQL = (
    f"WITH latest AS ({_LATEST})"
    " SELECT r.case_run_id, c.source_case_id, rm.release_id, r.release_manifest_ref, r.lane, l.state,"
    " r.created_at, l.created_at, "
    + ", ".join(_LAST_OK.format(task=task) for task in ("c02.pass2", "c03.primary_selection", "c04.routing"))
    + ' FROM case_run r JOIN "case" c ON c.case_id = r.case_id'
    " LEFT JOIN latest l ON l.case_run_id = r.case_run_id"
    " LEFT JOIN release_manifest rm ON rm.manifest_ref = r.release_manifest_ref"
    " LEFT JOIN case_run_event a ON a.case_run_id = r.case_run_id AND a.state = 'ADMITTED'"
    " WHERE (%(state)s::text[] IS NULL OR l.state = ANY(%(state)s))"
    " AND (%(release)s::text IS NULL OR rm.release_id = %(release)s)"
    " AND (%(q)s::text IS NULL OR c.source_case_id ILIKE %(like)s OR r.case_run_id = %(q)s)"
    " AND (%(admitted_from)s::date IS NULL OR r.created_at >= %(admitted_from)s::date)"
    " AND (%(admitted_to)s::date IS NULL OR r.created_at < %(admitted_to)s::date + 1)"
    " AND (%(transcript)s::text IS NULL OR coalesce(a.detail->>'transcript', '') = %(transcript)s)"
    " AND (%(baseline)s::text IS NULL OR (coalesce(a.detail->>'baseline', '') = 'present') = (%(baseline)s = 'present'))"
    " AND (%(failed_at)s::text IS NULL OR (EXISTS (SELECT 1 FROM task_execution f WHERE f.case_run_id = r.case_run_id"
    " AND f.logical_task_id = %(failed_at)s AND f.status IN ('FAILED_RETRYABLE', 'FAILED_PERMANENT'))"
    " AND NOT EXISTS (SELECT 1 FROM task_execution s WHERE s.case_run_id = r.case_run_id"
    " AND s.logical_task_id = %(failed_at)s AND s.status = 'SUCCEEDED')))"
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
    claims: int | None = None  # from the fact index, on the list only; None: no Pass 2 result indexed

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


@dataclass(frozen=True)
class Step:
    """One stage of the line on the case page's tracker (the DLS Multi-Step Tracker)."""

    label: str
    kind: str  # done, current, failed, todo
    at: datetime | None = None
    gap: float | None = None  # seconds since the step before it
    detail: Mapping[str, Any] = field(default_factory=dict)
    ended: datetime | None = None  # when it failed or was cancelled in this stage


TERMINAL = ("PLAN_READY", "FAILED", "CANCELLED")


def steps(run: CaseRun) -> list[Step]:
    """Every stage of the line, in order: the ones reached with their time, the latest one current
    while the run goes on, the one it failed or was cancelled in marked so, then the ones not reached."""
    reached = {e.state: e for e in run.events}
    last = run.events[-1].state if run.events else None
    found: list[Step] = []
    previous: datetime | None = None
    for state, label in LINE:
        event = reached.get(state)
        if event is None:
            found.append(Step(label, "todo"))
            continue
        kind = "current" if state == last and last not in TERMINAL else "done"
        found.append(Step(label, kind, event.at, _gap(previous, event.at), event.detail))
        previous = event.at
    stopped = next((reached[s] for s in ("FAILED", "CANCELLED") if s in reached), None)
    if stopped is not None:
        kind = "failed" if stopped.state == "FAILED" else "stopped"
        where = max((i for i, step in enumerate(found) if step.kind != "todo"), default=None)
        if where is None:
            found.insert(0, Step(STATE_LABELS[stopped.state], kind, stopped.at, None, stopped.detail))
        else:  # the stage it was in when it failed or was cancelled
            step = found[where]
            found[where] = Step(step.label, kind, step.at, step.gap, {**step.detail, **stopped.detail}, stopped.at)
    return found


def _gap(before: datetime | None, at: datetime) -> float | None:
    return None if before is None else (at - before).total_seconds()


def _row(r: Sequence[Any]) -> CaseRow:
    return CaseRow(*r)


# --- the list's filters (LDR-0037) -------------------------------------------------------------

FAILED_AT = {task: label for task, label in STAGE_LABELS.items() if task != "c05.planning"}
TRANSCRIPT = {"present": "With a transcript", "absent": "Without a transcript"}
BASELINE = {"present": "With customer accounts", "absent": "Without customer accounts"}
PRIMARY = {"SELECTED": "Selected", "AMBIGUOUS": "Ambiguous", "NO_ELIGIBLE_CLAIM": "No eligible claim"}
GROUNDING = {
    "GROUNDED": "Grounded",
    "PARTIAL": "Partial",
    "EVENT_TIME_APPROXIMATE": "Event time approximate",
    "UNGROUNDED": "Ungrounded",
}
ACCOUNTS = {
    "RESOLVED": "One account",
    "CANDIDATES": "Several possible accounts",
    "NONE": "No account named",
    "NOT_APPLICABLE": "Not about an account",
}
ROUTING_OUTCOMES = ("UNMAPPED", "NO_ELIGIBLE_VERTICAL")
MAX_CLAIMS = 99


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip()) if value else None
    except ValueError:
        return None


def _count(value: str | None) -> int | None:
    try:
        n = int((value or "").strip())
    except ValueError:
        return None
    return n if 0 <= n <= MAX_CLAIMS else None


def _one_of(value: str | None, allowed: Mapping[str, str] | Sequence[str]) -> str | None:
    return value if value in allowed else None


def _values(params: Mapping[str, Any], name: str) -> list[str]:
    """Every value of ``name``: repeated parameters (a multi-select) or one comma-separated value
    (the links the page builds)."""
    raw = params.getlist(name) if hasattr(params, "getlist") else [params.get(name)]
    return [v.strip() for item in raw if item for v in str(item).split(",") if v.strip()]


def _many(params: Mapping[str, Any], name: str, allowed: Mapping[str, str] | Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v for v in _values(params, name) if v in allowed))


@dataclass(frozen=True)
class CaseFilters:
    """What the Cases page filters on. Values not on offer are dropped, never passed to SQL."""

    q: str = ""
    state: tuple[str, ...] = ()  # any of these stages (a multi-select)
    admitted_from: date | None = None
    admitted_to: date | None = None
    transcript: str | None = None
    baseline: str | None = None
    failed_at: str | None = None
    claims_min: int | None = None
    claims_max: int | None = None
    primary: str | None = None
    vertical: tuple[str, ...] = ()  # a claim routed to any of these (a multi-select)
    grounding: str | None = None
    account: str | None = None

    RESULT_FIELDS = ("claims_min", "claims_max", "primary", "vertical", "grounding", "account")
    MORE_FIELDS = ("admitted_from", "admitted_to", "transcript", "baseline", "failed_at", *RESULT_FIELDS)

    @classmethod
    def parse(cls, params: Mapping[str, Any]) -> CaseFilters:
        claims_min, claims_max = _count(params.get("claims_min")), _count(params.get("claims_max"))
        if claims_min is not None and claims_max is not None and claims_max < claims_min:
            claims_min, claims_max = claims_max, claims_min
        start, end = _date(params.get("admitted_from")), _date(params.get("admitted_to"))
        if start and end and end < start:
            start, end = end, start
        verticals = tuple(dict.fromkeys(v for v in _values(params, "vertical") if len(v) <= 80))[:20]
        return cls(
            q=(params.get("q") or "").strip(),
            state=_many(params, "state", STATES),
            admitted_from=start,
            admitted_to=end,
            transcript=_one_of(params.get("transcript"), TRANSCRIPT),
            baseline=_one_of(params.get("baseline"), BASELINE),
            failed_at=_one_of(params.get("failed_at"), FAILED_AT),
            claims_min=claims_min,
            claims_max=claims_max,
            primary=_one_of(params.get("primary"), PRIMARY),
            vertical=verticals,
            grounding=_one_of(params.get("grounding"), GROUNDING),
            account=_one_of(params.get("account"), ACCOUNTS),
        )

    @property
    def on_results(self) -> bool:
        """Whether a filter needs the stage results (the fact index), not just Postgres."""
        return any(getattr(self, name) not in (None, ()) for name in self.RESULT_FIELDS)

    @property
    def more(self) -> int:
        """How many of the "More filters" are set; a date or claims range counts once."""
        return len(self.chips())

    @property
    def active(self) -> bool:
        return bool(self.q) or bool(self.state) or self.more > 0

    def query(self, **changes: Any) -> dict[str, str]:
        """The filters as URL parameters, with ``changes``; unset ones left out."""
        values = {f.name: getattr(self, f.name) for f in fields(self)} | changes
        return {
            k: ",".join(v) if isinstance(v, tuple) else str(v) for k, v in values.items() if v not in (None, "", ())
        }

    def chips(self) -> list[tuple[str, tuple[str, ...]]]:
        """The "More filters" in use, as (label, the parameters removing it clears)."""
        found: list[tuple[str, tuple[str, ...]]] = []
        start, end = self.admitted_from, self.admitted_to
        if start or end:
            label = (
                f"Admitted {start} to {end}"
                if start and end
                else f"Admitted from {start}"
                if start
                else f"Admitted by {end}"
            )
            found.append((label, ("admitted_from", "admitted_to")))
        for name, labels in (("transcript", TRANSCRIPT), ("baseline", BASELINE)):
            value = getattr(self, name)
            if value:
                found.append((labels[value], (name,)))
        if self.failed_at:
            found.append((f"Failed at {FAILED_AT[self.failed_at]}", ("failed_at",)))
        lo, hi = self.claims_min, self.claims_max
        if lo is not None or hi is not None:
            if lo == hi:
                label = f"{lo} claim{'' if lo == 1 else 's'}"
            elif hi is None:
                label = f"{lo} or more claims"
            elif lo is None:
                label = f"Up to {hi} claim{'' if hi == 1 else 's'}"
            else:
                label = f"{lo} to {hi} claims"
            found.append((label, ("claims_min", "claims_max")))
        if self.primary:
            found.append((f"Primary claim: {PRIMARY[self.primary].lower()}", ("primary",)))
        if self.vertical:
            found.append((f"Vertical: {', '.join(self.vertical)}", ("vertical",)))
        if self.grounding:
            found.append((f"A claim {GROUNDING[self.grounding].lower()}", ("grounding",)))
        if self.account:
            found.append((f"A claim: {ACCOUNTS[self.account].lower()}", ("account",)))
        return found

    def matches(self, facts: Mapping[str, Mapping[str, Any]]) -> bool:
        """``facts`` by task. A result filter needs that stage's result: a run without it doesn't match."""
        pass2, c03, c04 = (facts.get(t) for t in ("c02.pass2", "c03.primary_selection", "c04.routing"))
        if self.claims_min is not None or self.claims_max is not None or self.grounding or self.account:
            if pass2 is None:
                return False
            n = int(pass2.get("claims", 0))
            if self.claims_min is not None and n < self.claims_min:
                return False
            if self.claims_max is not None and n > self.claims_max:
                return False
            if self.grounding and self.grounding not in pass2.get("grounding", ()):
                return False
            if self.account and self.account not in pass2.get("accounts", ()):
                return False
        if self.primary and (c03 is None or c03.get("primary") != self.primary):
            return False
        return not (self.vertical and (c04 is None or not set(self.vertical) & set(c04.get("verticals", ()))))


@dataclass(frozen=True)
class CasePage:
    rows: list[CaseRow]
    more: bool  # is there a next page
    unindexed: int = 0  # runs with a result the fact index doesn't hold yet: result filters can't see them


def list_cases(
    db, *, release: str | None = None, filters: CaseFilters | None = None, offset: int = 0, facts=None
) -> CasePage:
    """A page of case runs, newest first. ``q`` matches a case number (part of it) or a run id.

    Filters Postgres holds go into the SQL. Result filters read ``facts`` (a ``FactIndex``): every
    run the SQL keeps is checked, then paged here.
    """
    filters = filters or CaseFilters()
    q = filters.q or None
    offset = max(0, offset)
    on_results = filters.on_results and facts is not None
    params = {
        "state": list(filters.state) or None,
        "release": release or None,
        "q": q,
        "like": f"%{q}%" if q else None,
        "admitted_from": filters.admitted_from,
        "admitted_to": filters.admitted_to,
        "transcript": filters.transcript,
        "baseline": filters.baseline,
        "failed_at": filters.failed_at,
        "limit": None if on_results else PAGE_ROWS + 1,  # one more: is there a next page
        "offset": 0 if on_results else offset,
    }
    with db.transaction() as tx:
        found = tx.all(CASES_SQL, params)
    known = facts.get(ref for r in found for ref in r[8:11]) if facts is not None else {}
    unindexed = sum(1 for r in found for ref in r[8:11] if ref and ref not in known)
    rows: list[CaseRow] = []
    for r in found:
        by_task = {
            task: known[ref]
            for task, ref in zip(("c02.pass2", "c03.primary_selection", "c04.routing"), r[8:11], strict=True)
            if ref in known
        }
        if on_results and not filters.matches(by_task):
            continue
        claims = by_task.get("c02.pass2", {}).get("claims")
        rows.append(CaseRow(*r[:8], claims=claims))
    if on_results:
        page = rows[offset : offset + PAGE_ROWS + 1]
        return CasePage(page[:PAGE_ROWS], len(page) > PAGE_ROWS, unindexed)
    return CasePage(rows[:PAGE_ROWS], len(rows) > PAGE_ROWS, unindexed)


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


def read_baseline(db, store, run: CaseRun, pass2: Any) -> bytes:
    """The baseline profile Pass 2 was given: the customer accounts and fields it saw.

    Only the ref the run's own Pass 2 result names, and only INTERNAL; anything else raises
    ``NotShown``. Never the ref map (RESTRICTED), which holds the identifiers.
    """
    ref = pass2.get("baseline_profile_ref") if isinstance(pass2, Mapping) else None
    pass2_refs = {r for s in run.stages if s.task == "c02.pass2" for r in s.refs}
    if not isinstance(ref, str) or not ref.startswith("sha256:") or not pass2_refs:
        raise NotShown(str(ref))
    with db.transaction() as tx:
        stored = set(tx.column(CLASSIFICATIONS_SQL, {"sha": ref.removeprefix("sha256:")}))
    if Classification.INTERNAL.value not in stored:
        raise NotShown(ref)
    return store.get(ref, classification=Classification.INTERNAL, authorization=READER)


# The baseline's fields, in the order the case page shows them (BaselineAccount).
ACCOUNT_FIELDS = (
    ("product", "Product"),
    ("co_brand_partner", "Co-brand"),
    ("business_unit", "Business unit"),
    ("account_type", "Type"),
    ("status_at_case", "Status at case"),
    ("opened_month", "Opened"),
    ("card_replaced_month", "Card replaced"),
    ("holder", "Holder"),
    ("card_role", "Card role"),
    ("role_detail", "Role detail"),
    ("market", "Market"),
)


@dataclass(frozen=True)
class CustomerData:
    """What Pass 2 was given about the customer, shaped for the case page."""

    received: str | None  # the case received date Pass 2 saw
    temporal_quality: str | None
    accounts: list[Mapping[str, Any]]
    columns: list[tuple[str, str]]  # only the fields some account has
    claims_by_account: dict[str, int]  # account ref → how many claims name it

    def label(self, ref: str) -> str:
        """``A1 · Platinum Card`` (the product, when known)."""
        account = next((a for a in self.accounts if a.get("account_ref") == ref), None)
        product = (account or {}).get("product")
        return f"{ref} · {product}" if product else ref

    def is_case_account(self, ref: str) -> bool:
        return any(a.get("account_ref") == ref and a.get("is_case_account") for a in self.accounts)


def customer_data(pass2: Mapping[str, Any], baseline: Any) -> CustomerData:
    accounts = (
        [a for a in (baseline or {}).get("accounts", ()) if isinstance(a, Mapping)]
        if isinstance(baseline, Mapping)
        else []
    )
    columns = [(key, label) for key, label in ACCOUNT_FIELDS if any(a.get(key) for a in accounts)]
    used: dict[str, int] = {}
    for claim in pass2.get("claims", ()):
        for ref in (claim.get("account_refs") or ()) if isinstance(claim, Mapping) else ():
            used[ref] = used.get(ref, 0) + 1
    received = pass2.get("case_received_date") or (
        baseline.get("case_received_date") if isinstance(baseline, Mapping) else None
    )
    quality = baseline.get("temporal_quality") if isinstance(baseline, Mapping) else None
    return CustomerData(received, quality, accounts, columns, used)


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
    "CaseFilters",
    "CasePage",
    "CaseRow",
    "CaseRun",
    "CustomerData",
    "NotShown",
    "Release",
    "Withheld",
    "blockers",
    "customer_data",
    "list_cases",
    "logs_url",
    "parse_result",
    "read_baseline",
    "read_case",
    "read_releases",
    "read_result",
    "steps",
    "trace_url",
]
