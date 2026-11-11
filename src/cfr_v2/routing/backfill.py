"""Route claim sets that runs already produced, outside the workflow (LDR-0022, open item).

On 2026-11-10 about 8,600 runs of release ``cfr-v2-phase-d`` went through C02 and C03, then failed at
routing, which that release can't do. Their claim sets are stored. This routes them with a current
release's pins, exactly as the stage would (``route_set``), without running C02 again:

    python -m cfr_v2.routing backfill --from-release cfr-v2-phase-d --count-only
    python -m cfr_v2.routing backfill --from-release cfr-v2-phase-d --limit 20 --out backfill.jsonl
    python -m cfr_v2.routing backfill --from-release cfr-v2-phase-d --out backfill.jsonl

Each ``RoutingResult`` is stored as an INTERNAL artifact and registered. ``--out`` gets one line per
run: ids, refs, counts and verticals, never claim text. A run already in ``--out`` is skipped, so
an interrupted backfill resumes. The runs stay FAILED: these results are an evaluation set, not
production state. ``--count-only`` makes no model call.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cfr_v2.contracts import Classification, RoutingResult, canonical_json
from cfr_v2.telemetry import get_logger

from .handler import RoutingContext, load_claim_set, route_set
from .route import decision
from .router import ROUTE_PROGRAM_ID, ROUTE_VERSION

logger = get_logger(__name__)

PENDING_SQL = (
    "SELECT c.case_run_id, c.case_id, t.output_artifact_refs->>0"
    " FROM case_run c"
    " JOIN workflow_job j USING (case_run_id)"
    " JOIN task_execution t ON t.case_run_id = c.case_run_id AND t.logical_task_id = 'c02.pass2'"
    " WHERE j.status = 'FAILED' AND c.release_manifest_ref LIKE %(release)s"
    " AND NOT EXISTS (SELECT 1 FROM task_execution r"
    "                 WHERE r.case_run_id = c.case_run_id AND r.logical_task_id = 'c04.routing')"
    " ORDER BY c.created_at, c.case_run_id"
)


@dataclass(frozen=True)
class PendingRun:
    case_run_id: str
    case_id: str
    claim_set_ref: str


@dataclass
class Outcome:
    run: PendingRun
    claims: int = 0
    routing_ref: str | None = None
    decisions: Counter | None = None
    error: str | None = None


def already_done(out: Path | None) -> set[str]:
    if out is None or not out.is_file():
        return set()
    done = set()
    for line in out.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("routing_ref"):  # failed runs are tried again
                done.add(row["case_run_id"])
    return done


async def backfill(
    runs: Sequence[PendingRun],
    store,
    context: RoutingContext,
    lm,
    *,
    concurrency: int = 4,
    register: Callable[[object], None] = lambda stored: None,
    write: Callable[[dict], None] = lambda row: None,
) -> list[Outcome]:
    """Route each run's claim set; a failed run is recorded and the rest go on."""
    gate = asyncio.Semaphore(concurrency)

    async def one(run: PendingRun) -> Outcome:
        outcome = Outcome(run)
        async with gate:
            try:
                claim_set = await asyncio.to_thread(load_claim_set, store, run.claim_set_ref)
                outcome.claims = len(claim_set.claims)
                routed = await route_set(claim_set, context, lm) if claim_set.claims else []
                result = RoutingResult(
                    claim_set_hash=claim_set.claim_set_hash,
                    program_id=ROUTE_PROGRAM_ID,
                    program_version=ROUTE_VERSION,
                    claims=tuple(r.routing for r in routed),
                    procedures_ref=context.procedures_ref,
                    verticals_sha256=context.catalog.sha256,
                )
                stored = await asyncio.to_thread(
                    store.put,
                    canonical_json(result),
                    media_type="application/json",
                    classification=Classification.INTERNAL,
                )
                await asyncio.to_thread(register, stored)
                outcome.routing_ref = stored.metadata.ref
                outcome.decisions = Counter(decision(r.routing) for r in routed)
            except Exception as exc:  # noqa: BLE001 - one run's failure is recorded; the type only
                outcome.error = type(exc).__name__
                logger.warning("backfill: run %s failed (%s)", run.case_run_id, outcome.error)
        write(
            {
                "case_run_id": run.case_run_id,
                "case_id": run.case_id,
                "claims": outcome.claims,
                "routing_ref": outcome.routing_ref,
                "decisions": dict(outcome.decisions or {}),
                "error": outcome.error,
            }
        )
        return outcome

    return list(await asyncio.gather(*(one(r) for r in runs)))


def summary(outcomes: Sequence[Outcome]) -> list[str]:
    decisions: Counter = Counter()
    for o in outcomes:
        decisions.update(o.decisions or {})
    failed = Counter(o.error for o in outcomes if o.error)
    routed = sum(o.routing_ref is not None for o in outcomes)
    claims = sum(o.claims for o in outcomes)
    lines = [f"{len(outcomes)} runs: {routed} routed, {sum(failed.values())} failed; {claims} claims"]
    lines += [f"  {name:<44}{count:>6}" for name, count in decisions.most_common()]
    if failed:
        lines.append("failures: " + ", ".join(f"{k} {v}" for k, v in failed.most_common()))
    return lines
