"""C02 trial: run Pass 1 and Pass 2 on a few admitted cases, outside the workflow.

    python -m cfr_v2.understanding --sample 5 --out c02_trial
    python -m cfr_v2.understanding --case-run-id <id> --case-run-id <id> --out c02_trial
    python -m cfr_v2.understanding --sample 5 --pass1-only

Reads admitted requests, transcripts and baselines as the worker would and calls the real
gateway, but writes nothing to the state database or the artifact store (Pass 1's result
is kept in memory for Pass 2). Prints counts, latency and tokens per case and pass, plus a
cost projection; with ``--out``, writes one review file per case: Pass 1's items with the
redacted text their spans cover, then Pass 2's claims, other checks and findings.

Review files hold redacted transcript excerpts (INTERNAL): keep them on this machine
and delete them after review.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from cfr_v2.artifacts import ArtifactNotFoundError, InMemoryArtifactStore, S3ArtifactStore, S3Settings, StoredArtifact
from cfr_v2.contracts import AuthorizationContext, ClaimSetRevision, Classification, Pass1Result
from cfr_v2.llm import GatewayModelClient, GatewaySettings, ModelError, ModelRequest, ModelResponse, close_model_loop
from cfr_v2.llm.dspy_lm import gateway_lm
from cfr_v2.registry.evidence_sources import PinnedTaxonomies
from cfr_v2.runtime.settings import RuntimeSettings
from cfr_v2.runtime.stages import STAGES, StageInput
from cfr_v2.state import Database
from cfr_v2.telemetry import init_telemetry, shutdown_telemetry

from .handler import load_segments, make_pass1_handler, make_pass2_handler
from .pass1 import PASS1_PROGRAM_ID


class _Overlay:
    """Reads from the real store; writes stay in memory, so a trial leaves nothing behind."""

    def __init__(self, base: S3ArtifactStore) -> None:
        self.base = base
        self.memory = InMemoryArtifactStore()

    def put(self, data: bytes, *, media_type: str, classification: Classification) -> StoredArtifact:
        return self.memory.put(data, media_type=media_type, classification=classification)

    def get(self, ref: str, *, classification: Classification, authorization: AuthorizationContext) -> bytes:
        try:
            return self.memory.get(ref, classification=classification, authorization=authorization)
        except ArtifactNotFoundError:
            return self.base.get(ref, classification=classification, authorization=authorization)


class _Metered:
    """Records token usage per case and pass while delegating to the gateway client."""

    def __init__(self, inner: GatewayModelClient) -> None:
        self.inner = inner
        self.usage: dict[tuple[str, int], list[int]] = {}

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self.inner.complete(request)
        totals = self.usage.setdefault((request.case_id, 1 if request.program_id == PASS1_PROGRAM_ID else 2), [0, 0])
        totals[0] += response.usage.input_tokens
        totals[1] += response.usage.output_tokens
        return response


@dataclass
class _Outcome:
    run_id: str
    pass1: Pass1Result | None = None
    claim_set: ClaimSetRevision | None = None
    seconds: list[float] = field(default_factory=list)
    error: str = ""


def _case_runs(db: Database, ids: list[str], sample: int) -> list[tuple[str, str, str]]:
    columns = "case_run_id, request_ref, release_manifest_ref"
    with db.transaction() as tx:
        if ids:
            rows = tx.all(f"SELECT {columns} FROM case_run WHERE case_run_id = ANY(%s)", (ids,))
        else:
            rows = tx.all(f"SELECT {columns} FROM case_run ORDER BY random() LIMIT %s", (sample,))
    return [(r[0], r[1], r[2]) for r in rows]


def _review(result: Pass1Result, segments: dict[str, str], claim_set: ClaimSetRevision | None) -> dict:
    def quoted(span) -> str:
        return segments[span.interaction_id][span.start : span.end]

    def item(obj, exclude=frozenset()) -> dict:
        return {**obj.model_dump(mode="json", exclude={"spans", *exclude}), "quoted": [quoted(s) for s in obj.spans]}

    review = {
        "case_id": result.case_id,
        "pass1_version": result.program_version,
        "pass1_dropped": [d.model_dump(mode="json") for d in result.dropped],
        "allegations": [
            {
                **item(a, {"details"}),
                "details": [
                    {"type": d.detail_type.value, "detail": d.text, "quoted": quoted(d.span)} for d in a.details
                ],
            }
            for a in result.allegations
        ],
        "requests": [item(r) for r in result.requests],
        "agent_statements": [item(s) for s in result.agent_statements],
    }
    if claim_set is not None:
        review |= {
            "case_received_date": claim_set.case_received_date and claim_set.case_received_date.isoformat(),
            "baseline": "present" if claim_set.baseline_profile_ref else "absent",
            "evidence_sources_version": claim_set.evidence_sources_version,
            "claims": [c.model_dump(mode="json") for c in claim_set.claims],
            "other_checks": [o.model_dump(mode="json") for o in claim_set.other_checks],
            "pass2_findings": [f.model_dump(mode="json") for f in claim_set.findings],
        }
    return review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.understanding", description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=int, default=5, help="random admitted case runs to try (default 5)")
    parser.add_argument("--case-run-id", action="append", default=[], help="specific case run (repeatable)")
    parser.add_argument("--out", type=Path, help="directory for per-case review files")
    parser.add_argument("--workers", type=int, default=8, help="cases run in parallel (default 8)")
    parser.add_argument("--pass1-only", action="store_true", help="stop after Pass 1")
    parser.add_argument("--total-cases", type=int, default=10358, help="cases to project the token cost for")
    args = parser.parse_args(argv)

    init_telemetry(service_name="cfr-v2-c02-trial")
    db = Database(RuntimeSettings.from_env().state_database_url, max_connections=2)
    store = _Overlay(S3ArtifactStore(S3Settings.from_env()))
    settings = GatewaySettings.from_env()
    client = _Metered(GatewayModelClient(settings))
    lm = gateway_lm(client, settings.model_id)
    taxonomies = PinnedTaxonomies(store, db)  # each run's pinned release manifest names its taxonomy
    pass1_handler = make_pass1_handler(store, lm)
    pass2_handler = make_pass2_handler(store, lm, taxonomies)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)

    def run(case_run: tuple[str, str, str]) -> _Outcome:
        run_id, request_ref, manifest_ref = case_run
        outcome = _Outcome(run_id)
        try:
            started = time.monotonic()
            out = pass1_handler(StageInput(run_id, STAGES[0], request_ref, (), release_manifest_ref=manifest_ref))
            outcome.seconds.append(time.monotonic() - started)
            outcome.pass1 = Pass1Result.model_validate_json(out.content)
            if not args.pass1_only:
                stored = store.put(out.content, media_type=out.media_type, classification=Classification.INTERNAL)
                started = time.monotonic()
                out = pass2_handler(
                    StageInput(
                        run_id, STAGES[1], request_ref, (stored.metadata.ref,), release_manifest_ref=manifest_ref
                    )
                )
                outcome.seconds.append(time.monotonic() - started)
                outcome.claim_set = ClaimSetRevision.model_validate_json(out.content)
        except ModelError as exc:
            outcome.error = f"pass {len(outcome.seconds) + 1}: {type(exc).__name__}"
        if args.out and outcome.pass1 is not None and outcome.pass1.transcript_ref is not None:
            segments = dict(load_segments(store, outcome.pass1.transcript_ref))
            review = json.dumps(_review(outcome.pass1, segments, outcome.claim_set), indent=2, ensure_ascii=False)
            (args.out / f"{outcome.pass1.case_id}.json").write_text(review + "\n")
        return outcome

    try:
        runs = _case_runs(db, args.case_run_id, args.sample)
        passes = "Pass 1" if args.pass1_only else "Pass 1 + Pass 2"
        print(f"{passes} on {len(runs)} case runs, model {settings.model_id!r}")
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            outcomes = list(pool.map(run, runs))
    finally:
        close_model_loop()
        db.close()
        shutdown_telemetry()

    _print_pass1(outcomes, client)
    if not args.pass1_only:
        _print_pass2(outcomes, client)
    for n in (1, 2):
        called = [u for (case, p), u in client.usage.items() if p == n and u[0]]
        if called:
            avg_in = sum(u[0] for u in called) / len(called)
            avg_out = sum(u[1] for u in called) / len(called)
            print(
                f"pass {n} average per case: {avg_in:,.0f} in / {avg_out:,.0f} out tokens; for {args.total_cases:,} "
                f"cases about {avg_in * args.total_cases / 1e6:,.1f}M in / {avg_out * args.total_cases / 1e6:,.1f}M out"
            )
    if args.out:
        print(f"review files in {args.out}/ (redacted excerpts: keep local, delete after review)")
    return 1 if any(o.error for o in outcomes) else 0


def _row(case: str, cells: tuple, error: str = "") -> str:
    return f"{case:<14}" + "".join(f"{c:>12}" for c in cells) + (f"  {error}" if error else "")


def _print_pass1(outcomes: list[_Outcome], client: _Metered) -> None:
    print("\nPass 1")
    print(_row("case", ("allegations", "details", "requests", "statements", "dropped", "seconds", "in tok", "out tok")))
    for o in outcomes:
        r = o.pass1
        case = r.case_id if r else o.run_id[:12]
        counts = (
            (
                len(r.allegations),
                sum(len(a.details) for a in r.allegations),
                len(r.requests),
                len(r.agent_statements),
                len(r.dropped),
            )
            if r
            else ("-",) * 5
        )
        seconds = f"{o.seconds[0]:.1f}" if o.seconds else "-"
        print(_row(case, (*counts, seconds, *client.usage.get((case, 1), [0, 0])), o.error if not r else ""))


def _print_pass2(outcomes: list[_Outcome], client: _Metered) -> None:
    print("\nPass 2")
    print(
        _row(
            "case", ("claims", "accounts", "windows", "needs", "other chk", "findings", "seconds", "in tok", "out tok")
        )
    )
    for o in outcomes:
        if o.pass1 is None:
            continue
        c, case = o.claim_set, o.pass1.case_id
        if c is None:
            print(_row(case, ("-",) * 6 + ("-",) + tuple(client.usage.get((case, 2), [0, 0])), o.error))
            continue
        resolved = sum(cl.account_resolution.value == "RESOLVED" for cl in c.claims)
        dated = sum(cl.time_quality.value != "UNKNOWN" for cl in c.claims)
        cells = (
            len(c.claims),
            f"{resolved}/{len(c.claims)}",
            f"{dated}/{len(c.claims)}",
            sum(len(cl.evidence_needs) for cl in c.claims),
            len(c.other_checks),
            len(c.findings),
            f"{o.seconds[1]:.1f}",
            *client.usage.get((case, 2), [0, 0]),
        )
        print(_row(case, cells))


if __name__ == "__main__":
    sys.exit(main())
