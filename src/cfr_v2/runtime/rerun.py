"""Re-run case runs from a stage, under the current release (LDR-0045).

    python -m cfr_v2.runtime.rerun --stage routing --from-release cfr-v2-phase-e --count-only
    python -m cfr_v2.runtime.rerun --stage routing --from-release cfr-v2-phase-e --case-no GC1 GC2 ...
    python -m cfr_v2.runtime.rerun --stage pass2 --runs-file runs.txt [--include-failed] [--out rerun.jsonl]

Each source run gets a **new case run** under the current release (``CFR_RELEASE_ID``): the same
case, request and lane, with the source's committed results for every stage before ``--stage``
copied in. It is queued, and the worker skips the copied stages, since their results are committed
for the same inputs and versions, and runs ``--stage`` and every stage after it. The source run is
left as it is.

A stage is re-run by not copying it, so a change the stage's inputs don't show (a new procedure
snapshot in the release, say) still takes effect.

A source run qualifies when it is Plan ready (or Failed, with ``--include-failed``), isn't queued or
running, and each stage before ``--stage`` succeeded at the version this code runs: a result made by
an older version isn't reused under a release that pins a newer one. Re-running from Pass 1 reuses
nothing, so any finished run qualifies. A re-run is unique per source, release and stage: asking
twice queues it once.

``--count-only`` writes nothing. Output is counts and ids, never case text.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cfr_v2.telemetry import current_traceparent, get_logger, link_to_current, set_attributes, span

from .stages import FINAL_STATE, STAGES, Stage
from .workflow import stage_input_hash

logger = get_logger(__name__)

NAMES = {  # what --stage accepts, besides the stage ids themselves
    "pass1": "c02.pass1",
    "pass2": "c02.pass2",
    "selection": "c03.primary_selection",
    "routing": "c04.routing",
    "planning": "c05.planning",
}
_ID_NAMESPACE = uuid.UUID("6f1c2a52-6a8e-4c5e-9d0e-2b7f3f6b9c11")  # the repository's, so ids share one space

# The source runs: the ids given, or the latest run per case under a release (as the evaluation
# exports choose), with their latest state and whether a worker holds or awaits them.
SOURCES_SQL = (
    "WITH chosen AS ("
    # given ids: each run as given, two runs of one case included
    "  SELECT r.case_run_id, r.created_at FROM case_run r"
    "  WHERE %(ids)s::text[] IS NOT NULL AND r.case_run_id = ANY(%(ids)s)"
    "  UNION ALL"
    # a release: its latest run per case, as the evaluation exports choose
    "  SELECT * FROM ("
    "   SELECT DISTINCT ON (c.source_case_id) r.case_run_id, r.created_at"
    '   FROM case_run r JOIN "case" c ON c.case_id = r.case_id'
    "   JOIN release_manifest rm ON rm.manifest_ref = r.release_manifest_ref"
    "   WHERE %(ids)s::text[] IS NULL AND rm.release_id = %(release)s"
    "   AND (%(cases)s::text[] IS NULL OR c.source_case_id = ANY(%(cases)s))"
    "   ORDER BY c.source_case_id, r.created_at DESC, r.case_run_id"
    "  ) per_case),"
    " picked AS (SELECT case_run_id FROM chosen"
    " ORDER BY CASE WHEN %(seed)s::text IS NULL THEN NULL ELSE md5(case_run_id || %(seed)s::text) END,"
    " created_at, case_run_id"
    " LIMIT %(limit)s),"
    " latest AS (SELECT DISTINCT ON (case_run_id) case_run_id, state FROM case_run_event"
    " WHERE case_run_id IN (SELECT case_run_id FROM picked) ORDER BY case_run_id, event_id DESC)"
    " SELECT r.case_run_id, c.source_case_id, r.case_id, r.ingestion_revision, r.lane, r.request_ref,"
    " rm.release_id, l.state, j.status"
    ' FROM picked p JOIN case_run r USING (case_run_id) JOIN "case" c ON c.case_id = r.case_id'
    " LEFT JOIN release_manifest rm ON rm.manifest_ref = r.release_manifest_ref"
    " LEFT JOIN latest l ON l.case_run_id = r.case_run_id"
    " LEFT JOIN workflow_job j ON j.case_run_id = r.case_run_id"
    " ORDER BY c.source_case_id, r.created_at"
)
RESULTS_SQL = (
    "SELECT case_run_id, logical_task_id, input_hash, program_version, status, output_artifact_refs"
    " FROM task_execution WHERE case_run_id = ANY(%(runs)s) AND plan_revision = 0"
)
EXISTING_SQL = (
    "SELECT rerun_of FROM case_run WHERE rerun_of = ANY(%(runs)s)"
    " AND release_manifest_ref = %(manifest)s AND rerun_from = %(stage)s"
)


class RerunRefused(ValueError):
    """The re-run can't be asked for as given; nothing was written."""


def stage_for(name: str) -> tuple[int, Stage]:
    """``--stage``: an id (``c04.routing``) or a name (``routing``)."""
    wanted = NAMES.get(name.lower(), name)
    for i, stage in enumerate(STAGES):
        if stage.logical_task_id == wanted:
            return i, stage
    raise RerunRefused(f"no stage {name!r}: one of {', '.join(NAMES)}")


def rerun_id(source_run_id: str, manifest_ref: str, stage_id: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"rerun:{source_run_id}:{manifest_ref}:{stage_id}"))


@dataclass(frozen=True)
class Result:
    logical_task_id: str
    input_hash: str
    program_version: str
    status: str
    output_artifact_refs: tuple[str, ...]


@dataclass(frozen=True)
class Source:
    case_run_id: str
    case_no: str
    case_id: str
    ingestion_revision: int
    lane: str
    request_ref: str
    release_id: str | None
    state: str | None
    job_status: str | None

    def row(self) -> dict[str, Any]:
        return {
            "case_run_id": self.case_run_id,
            "case_id": self.case_id,
            "ingestion_revision": self.ingestion_revision,
            "lane": self.lane,
            "request_ref": self.request_ref,
        }


def copy_chain(request_ref: str, results: Sequence[Result], start: int) -> list[Result] | str:
    """The results to copy for a re-run from ``STAGES[start]``, in stage order, or why there are
    none. Each must be the source's successful result for exactly the input the engine will compute
    (the request and the results before it), at the version this code runs."""
    upstream: list[str] = []
    chain: list[Result] = []
    for stage in STAGES[:start]:
        expected = stage_input_hash(request_ref, stage.logical_task_id, tuple(upstream))
        ok = [r for r in results if r.logical_task_id == stage.logical_task_id and r.status == "SUCCEEDED"]
        if not ok:
            return f"{stage.logical_task_id} never succeeded"
        matched = [r for r in ok if r.input_hash == expected and r.output_artifact_refs]
        current = [r for r in matched if r.program_version == stage.program_version]
        if not current:
            versions = sorted({r.program_version for r in ok} - {stage.program_version})
            if versions:
                return f"{stage.logical_task_id} is {', '.join(versions)}, this code runs {stage.program_version}"
            return f"{stage.logical_task_id}'s result doesn't follow from the stages before it"
        chain.append(current[0])
        upstream.append(current[0].output_artifact_refs[0])
    return chain


@dataclass
class Planned:
    source: Source
    status: str  # "rerun", "exists" (already re-run), "skipped"
    reason: str | None = None
    chain: list[Result] | None = None
    rerun_id: str | None = None


def plan(
    sources: Sequence[Source],
    results: Sequence[tuple[str, Result]],
    existing: set[str],
    *,
    start: int,
    manifest_ref: str,
    include_failed: bool,
) -> list[Planned]:
    """What would happen to each source run; pure, so the preview and the run agree."""
    by_run: dict[str, list[Result]] = {}
    for run_id, result in results:
        by_run.setdefault(run_id, []).append(result)
    stage_id = STAGES[start].logical_task_id
    allowed = {FINAL_STATE, "FAILED"} if include_failed else {FINAL_STATE}
    planned = []
    for s in sources:
        if s.job_status in ("QUEUED", "RUNNING"):
            planned.append(Planned(s, "skipped", "queued or running"))
            continue
        if s.state not in allowed:
            label = "failed" if s.state == "FAILED" else f"not Plan ready ({(s.state or 'no state').lower()})"
            planned.append(Planned(s, "skipped", label))
            continue
        if s.case_run_id in existing:
            planned.append(Planned(s, "exists", "already re-run from this stage under this release"))
            continue
        chain = copy_chain(s.request_ref, by_run.get(s.case_run_id, []), start)
        if isinstance(chain, str):
            planned.append(Planned(s, "skipped", chain))
            continue
        planned.append(Planned(s, "rerun", chain=chain, rerun_id=rerun_id(s.case_run_id, manifest_ref, stage_id)))
    return planned


def summary(planned: Sequence[Planned], stage: Stage, release_id: str) -> list[str]:
    """The lines the CLI prints and the console shows: counts and reasons only."""
    reruns = sum(p.status == "rerun" for p in planned)
    lines = [f"{reruns:,} runs to re-run from {stage.logical_task_id} under {release_id}"]
    reasons = Counter(p.reason for p in planned if p.status != "rerun")
    lines += [f"{n:,} skipped: {reason}" for reason, n in reasons.most_common()]
    return lines


def create(tx: Any, item: Planned, *, stage: Stage, manifest_ref: str, max_attempts: int) -> bool:
    """One re-run, in the caller's transaction: the run, its copied results, ADMITTED, and the job.
    False when it already existed (nothing else is written then)."""
    from cfr_v2.contracts import Lane, TaskResult, TaskStatus
    from cfr_v2.state import CaseStateRepository

    from . import jobs
    from .workflow import PLAN_REVISION

    repo = CaseStateRepository(tx)
    source = item.source
    if not repo.create_rerun(
        source.row(), case_run_id=item.rerun_id, release_manifest_ref=manifest_ref, rerun_from=stage.logical_task_id
    ):
        return False
    for result in item.chain or ():
        repo.commit_task_result(
            case_run_id=item.rerun_id,
            logical_task_id=result.logical_task_id,
            plan_revision=PLAN_REVISION,
            program_version=result.program_version,
            input_hash=result.input_hash,
            result=TaskResult(
                task_id=result.logical_task_id,
                status=TaskStatus.SUCCEEDED,
                output_artifact_refs=result.output_artifact_refs,
            ),
        )
    repo.record_state(item.rerun_id, "ADMITTED", {"rerun_of": source.case_run_id, "from": stage.logical_task_id})
    jobs.enqueue(tx, item.rerun_id, Lane(source.lane), max_attempts=max_attempts, trace_context=current_traceparent())
    return True


def read(
    db: Any,
    *,
    ids: Sequence[str] | None,
    release: str | None,
    cases: Sequence[str] | None,
    sample: int | None,
    seed: str | None,
    manifest_ref: str,
    stage_id: str,
) -> tuple[list[Source], list[tuple[str, Result]], set[str]]:
    """The source runs, their results and which are already re-run. No sample: every run chosen."""
    params = {
        "ids": list(ids) if ids is not None else None,
        "release": release,
        "cases": list(cases) if cases else None,
        "seed": seed if sample else None,
        "limit": sample,  # NULL: no limit
    }
    with db.transaction() as tx:
        rows = tx.all(SOURCES_SQL, params)
        sources = [Source(r[0], r[1], r[2], int(r[3]), r[4], r[5], r[6], r[7], r[8]) for r in rows]
        runs = [s.case_run_id for s in sources]
        results = [
            (r[0], Result(r[1], r[2], r[3], r[4], tuple(r[5] or ()))) for r in tx.all(RESULTS_SQL, {"runs": runs})
        ]
        existing = set(tx.column(EXISTING_SQL, {"runs": runs, "manifest": manifest_ref, "stage": stage_id}))
    return sources, results, existing


def _run_ids(path: Path) -> list[str]:
    ids = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return list(dict.fromkeys(i for i in ids if i and not i.startswith("#")))


def main(argv: list[str] | None = None) -> int:
    from cfr_v2.evaluation.__main__ import case_list

    parser = argparse.ArgumentParser(prog="python -m cfr_v2.runtime.rerun", description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True, help=f"the first stage to run again: {', '.join(NAMES)}")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--runs-file", type=Path, help="case run ids, one per line (the console writes this)")
    which.add_argument("--from-release", help="the latest run per case under this release")
    parser.add_argument("--sample", type=int, help="with --from-release: a random sample of N cases")
    parser.add_argument("--seed", help="the sample's seed, to repeat it")
    parser.add_argument("--case-no", nargs="*", help="with --from-release: only these case numbers")
    parser.add_argument("--cases-file", type=Path, help="with --from-release: case numbers in a .txt or .csv")
    parser.add_argument(
        "--include-failed", action="store_true", help="failed runs too, when the stages before succeeded"
    )
    parser.add_argument("--count-only", action="store_true", help="say what would be re-run; write nothing")
    parser.add_argument("--out", type=Path, help="one JSON line per source run: ids and outcome")
    args = parser.parse_args(argv)

    try:
        start, stage = stage_for(args.stage)
    except RerunRefused as exc:
        parser.exit(2, f"error: {exc}\n")
    ids = _run_ids(args.runs_file) if args.runs_file else None
    if args.runs_file and not ids:
        parser.exit(2, f"error: no case run ids in {args.runs_file}\n")
    cases = case_list(args.case_no or [], args.cases_file) if args.from_release else ()
    if args.runs_file and (args.sample or args.case_no or args.cases_file):
        parser.exit(2, "error: --sample and case numbers go with --from-release, not --runs-file\n")
    if args.sample and cases:
        parser.exit(2, "error: give a sample or case numbers, not both\n")
    if args.sample is not None and args.sample < 1:
        parser.exit(2, "error: --sample: at least 1\n")
    seed = (args.seed or uuid.uuid4().hex[:8]) if args.sample else None

    from cfr_v2.artifacts import S3ArtifactStore, S3Settings
    from cfr_v2.intake.settings import IntakeSettings
    from cfr_v2.registry import ReleaseRegistry, pin_manifest
    from cfr_v2.state import Database
    from cfr_v2.telemetry import init_telemetry, shutdown_telemetry

    from .release_check import IncompatibleReleaseError, require_compatible
    from .settings import RuntimeSettings

    intake = IntakeSettings.from_env()
    manifest = ReleaseRegistry(intake.registry_dir).load(intake.release_id)
    try:
        require_compatible(manifest)  # the worker would refuse its runs
    except IncompatibleReleaseError as exc:
        parser.exit(2, f"error: {exc}. Set CFR_RELEASE_ID to the current release\n")

    init_telemetry(service_name="cfr-v2-rerun")
    settings = RuntimeSettings.from_env()
    db = Database(settings.state_database_url, max_connections=2)
    try:
        manifest_ref = manifest.ref
        sources, results, existing = read(
            db,
            ids=ids,
            release=args.from_release,
            cases=cases,
            sample=args.sample,
            seed=seed,
            manifest_ref=manifest_ref,
            stage_id=stage.logical_task_id,
        )
        planned = plan(
            sources, results, existing, start=start, manifest_ref=manifest_ref, include_failed=args.include_failed
        )
        if ids is not None and len(sources) < len(ids):
            print(f"{len(ids) - len(sources):,} of the ids given are not case runs here", file=sys.stderr)
        if args.sample:
            print(f"a random sample of {args.sample} (seed {seed})")
        lines = summary(planned, stage, manifest.release_id)
        for line in lines[1:]:
            print(line)
        later = [s.logical_task_id for s in STAGES[start:]]
        print(
            f"stages run again: {', '.join(later)}; reused: {', '.join(s.logical_task_id for s in STAGES[:start]) or 'none'}"
        )
        if args.count_only:
            _write(args.out, planned, created=set())
            print(f"preview: {lines[0]}")
            return 0
        if any(p.status == "rerun" for p in planned):
            pin_manifest(manifest, store=S3ArtifactStore(S3Settings.from_env()), db=db)  # as intake: recorded once
        created: set[str] = set()
        for item in planned:
            if item.status != "rerun":
                continue
            with span(
                "case_run.rerun",
                new_trace=True,
                links=link_to_current(),
                case_no=item.source.case_no,
                source_run=item.source.case_run_id,
                rerun_from=stage.logical_task_id,
                manifest_ref=manifest_ref,
            ) as rerun_span:
                with db.transaction() as tx:
                    made = create(
                        tx, item, stage=stage, manifest_ref=manifest_ref, max_attempts=settings.max_job_attempts
                    )
                set_attributes(rerun_span, case_run_id=item.rerun_id, created=made)
            if made:
                created.add(item.rerun_id)
        _write(args.out, planned, created=created)
        print(f"{len(created):,} runs queued to re-run from {stage.logical_task_id} under {manifest.release_id}")
        return 0
    finally:
        db.close()
        shutdown_telemetry()


def _write(out: Path | None, planned: Sequence[Planned], *, created: set[str]) -> None:
    if out is None:
        return
    with out.open("w", encoding="utf-8") as handle:
        for p in planned:
            outcome = "queued" if p.rerun_id in created else p.status
            row = {"source": p.source.case_run_id, "case_no": p.source.case_no, "rerun": p.rerun_id, "outcome": outcome}
            if p.reason:
                row["reason"] = p.reason
            handle.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    sys.exit(main())
