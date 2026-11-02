"""Pass 1 trial: run C02 Pass 1 on a few admitted cases, outside the workflow.

    python -m cfr_v2.understanding --sample 5 --out pass1_trial
    python -m cfr_v2.understanding --case-run-id <id> --case-run-id <id> --out pass1_trial

Reads admitted requests and transcripts as the worker would, calls the real gateway,
and writes nothing to the state database or the artifact store. Prints counts,
latency and tokens per case, plus a cost projection; with ``--out``, writes one
review file per case: each allegation with the redacted text its spans cover.

Review files hold redacted transcript excerpts (INTERNAL): keep them on this machine
and delete them after review.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cfr_v2.artifacts import S3ArtifactStore, S3Settings
from cfr_v2.contracts import AllegationSet
from cfr_v2.llm import GatewayModelClient, GatewaySettings, ModelError, ModelRequest, ModelResponse, close_model_loop
from cfr_v2.llm.dspy_lm import gateway_lm
from cfr_v2.runtime.settings import RuntimeSettings
from cfr_v2.runtime.stages import STAGES, StageInput
from cfr_v2.state import Database
from cfr_v2.telemetry import init_telemetry, shutdown_telemetry

from .handler import load_segments, make_understanding_handler


class _Metered:
    """Records token usage per case while delegating to the gateway client."""

    def __init__(self, inner: GatewayModelClient) -> None:
        self.inner = inner
        self.usage: dict[str, list[int]] = {}

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self.inner.complete(request)
        totals = self.usage.setdefault(request.case_id, [0, 0])
        totals[0] += response.usage.input_tokens
        totals[1] += response.usage.output_tokens
        return response


def _case_runs(db: Database, ids: list[str], sample: int) -> list[tuple[str, str]]:
    with db.transaction() as tx:
        if ids:
            rows = tx.all("SELECT case_run_id, request_ref FROM case_run WHERE case_run_id = ANY(%s)", (ids,))
        else:
            rows = tx.all("SELECT case_run_id, request_ref FROM case_run ORDER BY random() LIMIT %s", (sample,))
    return [(r[0], r[1]) for r in rows]


def _review(result: AllegationSet, segments: dict[str, str]) -> dict:
    return {
        "case_id": result.case_id,
        "program_version": result.program_version,
        "dropped": [d.model_dump(mode="json") for d in result.dropped],
        "allegations": [
            {
                **a.model_dump(mode="json", exclude={"spans"}),
                "quoted": [segments[s.interaction_id][s.start : s.end] for s in a.spans],
            }
            for a in result.allegations
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.understanding", description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=int, default=5, help="random admitted case runs to try (default 5)")
    parser.add_argument("--case-run-id", action="append", default=[], help="specific case run (repeatable)")
    parser.add_argument("--out", type=Path, help="directory for per-case review files")
    parser.add_argument("--workers", type=int, default=8, help="cases run in parallel (default 8)")
    parser.add_argument("--total-cases", type=int, default=10358, help="cases to project the token cost for")
    args = parser.parse_args(argv)

    init_telemetry(service_name="cfr-v2-pass1-trial")
    db = Database(RuntimeSettings.from_env().state_database_url, max_connections=2)
    store = S3ArtifactStore(S3Settings.from_env())
    settings = GatewaySettings.from_env()
    client = _Metered(GatewayModelClient(settings))
    handler = make_understanding_handler(store, gateway_lm(client, settings.model_id))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)

    def run(case_run: tuple[str, str]) -> tuple[str, AllegationSet | None, float, str]:
        run_id, request_ref = case_run
        started = time.monotonic()
        try:
            out = handler(StageInput(run_id, STAGES[0], request_ref, ()))
        except ModelError as exc:
            return run_id, None, time.monotonic() - started, type(exc).__name__
        result = AllegationSet.model_validate_json(out.content)
        if args.out and result.transcript_ref is not None:
            segments = dict(load_segments(store, result.transcript_ref))
            review = json.dumps(_review(result, segments), indent=2, ensure_ascii=False)
            (args.out / f"{result.case_id}.json").write_text(review + "\n")
        return run_id, result, time.monotonic() - started, ""

    try:
        runs = _case_runs(db, args.case_run_id, args.sample)
        print(f"Pass 1 on {len(runs)} case runs, model {settings.model_id!r}")
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            results = list(pool.map(run, runs))
    finally:
        close_model_loop()
        db.close()
        shutdown_telemetry()

    print(f"{'case':<14}{'allegations':>12}{'dropped':>9}{'seconds':>9}{'in tok':>9}{'out tok':>9}  error")
    for run_id, result, took, error in results:
        case = result.case_id if result else run_id[:12]
        tokens = client.usage.get(case, [0, 0])
        counts = (len(result.allegations), len(result.dropped)) if result else ("-", "-")
        print(f"{case:<14}{counts[0]:>12}{counts[1]:>9}{took:>9.1f}{tokens[0]:>9}{tokens[1]:>9}  {error}")
    called = [u for u in client.usage.values() if u[0]]
    if called:
        avg_in = sum(u[0] for u in called) / len(called)
        avg_out = sum(u[1] for u in called) / len(called)
        print(
            f"average per case: {avg_in:,.0f} in / {avg_out:,.0f} out tokens; "
            f"for {args.total_cases:,} cases about {avg_in * args.total_cases / 1e6:,.1f}M in / "
            f"{avg_out * args.total_cases / 1e6:,.1f}M out"
        )
    if args.out:
        print(f"review files in {args.out}/ (redacted excerpts: keep local, delete after review)")
    return 1 if any(error for *_, error in results) else 0


if __name__ == "__main__":
    sys.exit(main())
