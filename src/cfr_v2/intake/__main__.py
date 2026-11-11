"""Batch intake: fetch cases for a resolution-date window, or a list of case numbers, and admit them.

    python -m cfr_v2.intake --start-date 2026-09-01 --end-date 2026-09-07
    python -m cfr_v2.intake --case-nos-from cases.xlsx [--sheet S] [--column case_no] [--missing-out missing.txt]
    python -m cfr_v2.intake --synthetic 20     # offline: synthetic rows, regex masker
    python -m cfr_v2.intake --synthetic 3 --seed 7  # new case numbers: seed 0's cases are duplicates now
    python -m cfr_v2.intake --print-sql > q.sql  # the BigQuery job's SQL, e.g. to check its cost

Runs as a submit-only process: it admits and enqueues; worker processes
(``python -m cfr_v2.runtime``) run the workflows. Configuration comes from the
environment (see ``.env.example``).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from cfr_v2.artifacts import S3ArtifactStore, S3Settings
from cfr_v2.registry import ReleaseRegistry
from cfr_v2.runtime import RuntimeSettings, start_runtime, stop_runtime
from cfr_v2.runtime.release_check import IncompatibleReleaseError, require_compatible
from cfr_v2.telemetry import get_logger, init_telemetry, shutdown_telemetry

from .bigquery import BigQueryCaseSource, BigQuerySettings
from .coordinator import IntakeCoordinator
from .redaction import EEUtilsMasker, Masker, ProfanityFilter
from .settings import IntakeSettings
from .source import CaseSource, RecordsCaseSource, case_query_sql, read_case_numbers

logger = get_logger("cfr_v2.intake.cli")  # not __name__: run as -m, that is "__main__", outside cfr_v2's logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.intake", description=__doc__.splitlines()[0])
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--synthetic", type=int, metavar="N", help="admit N synthetic rows instead of querying BigQuery")
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="synthetic rows' seed (default 0). Case numbers come from it, so a new seed admits new cases; "
        "the same seed again gives duplicates",
    )
    parser.add_argument(
        "--case-nos-from",
        type=Path,
        metavar="FILE",
        help="admit the cases listed in an .xlsx or .csv column, with the same query as a date window",
    )
    parser.add_argument("--column", default="case_no", help="the column holding case numbers (default case_no)")
    parser.add_argument("--sheet", help="the worksheet (default: the first)")
    parser.add_argument(
        "--missing-out",
        type=Path,
        metavar="FILE",
        help="write the listed case numbers the query didn't return, one per line (they are counted, never printed)",
    )
    parser.add_argument("--print-sql", action="store_true", help="print the case query (with the baseline unless CFR_INTAKE_BASELINE=0) and exit")
    parser.add_argument(
        "--allow-incompatible",
        action="store_true",
        help="admit even though the release's stage versions differ from this code's",
    )
    args = parser.parse_args(argv)
    if args.print_sql:
        print(
            case_query_sql(with_baseline=IntakeSettings.from_env().baseline, by_case_no=args.case_nos_from is not None)
        )
        return 0
    chosen = [args.synthetic is not None, args.case_nos_from is not None, args.start_date is not None]
    if sum(chosen) > 1:
        parser.error("give one of --synthetic, --case-nos-from, or --start-date/--end-date")
    if not any(chosen[:2]) and (args.start_date is None or args.end_date is None):
        parser.error("--start-date and --end-date are required unless --synthetic or --case-nos-from is given")
    case_nos = None
    if args.case_nos_from is not None:
        try:
            case_nos = read_case_numbers(args.case_nos_from, column=args.column, sheet=args.sheet)
        except (ValueError, KeyError) as exc:
            parser.error(str(exc))
        if not case_nos:
            parser.error(f"{args.case_nos_from.name}: no case numbers in column {args.column!r}")
        print(f"{len(case_nos)} distinct case numbers in {args.case_nos_from.name}", file=sys.stderr)

    init_telemetry(service_name="cfr-v2-intake")  # ee_config resolves its file, as for redaction
    settings = IntakeSettings.from_env()
    manifest = ReleaseRegistry(settings.registry_dir).load(settings.release_id)
    try:
        require_compatible(manifest)  # its runs would call the models, then fail (LDR-0022)
    except IncompatibleReleaseError as exc:
        if not args.allow_incompatible:
            shutdown_telemetry()
            parser.exit(2, f"error: {exc}. Set CFR_RELEASE_ID to the current release, or pass --allow-incompatible\n")
        logger.warning("intake: admitting anyway (--allow-incompatible): %s", exc)
    masker: Masker
    source: CaseSource
    if args.synthetic is not None:
        from .fakes import RegexMasker, synthetic_names, synthetic_rows

        rows = synthetic_rows(args.synthetic, seed=args.seed, baseline=settings.baseline)
        masker, source = RegexMasker(synthetic_names()), RecordsCaseSource(rows)
    else:
        masker, source = (
            EEUtilsMasker(),
            BigQueryCaseSource(BigQuerySettings.from_env(), with_baseline=settings.baseline, case_nos=case_nos),
        )

    store = S3ArtifactStore(S3Settings.from_env())
    start_runtime(RuntimeSettings.from_env(), store, listen_lanes=())
    try:
        coordinator = IntakeCoordinator.pinned(
            manifest,
            store=store,
            masker=masker,
            profanity=ProfanityFilter.from_file(settings.toxic_words_file),
            max_workers=settings.max_workers,
        )
        today = date.today()
        report = coordinator.admit_rows(source.fetch(start_date=args.start_date or today, end_date=args.end_date or today))
    finally:
        stop_runtime()
        shutdown_telemetry()
    summary = {"manifest_ref": report.manifest_ref, **report.summary()}
    if case_nos is not None:
        returned = {r.source_case_id for r in report.results if r.source_case_id}
        missing = [n for n in case_nos if n not in returned]
        summary |= {"requested": len(case_nos), "not_returned": len(missing)}
        if args.missing_out is not None:
            args.missing_out.write_text("".join(f"{n}\n" for n in missing), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
