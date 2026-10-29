"""Batch intake: fetch cases for a resolution-date window and admit them.

    python -m cfr_v2.intake --start-date 2026-09-01 --end-date 2026-09-07
    python -m cfr_v2.intake --synthetic 20     # offline: synthetic rows, regex masker

Runs as a submit-only process: it admits and enqueues; worker processes run the
workflows. Configuration comes from the environment (see ``.env.example``).
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from cfr_v2.artifacts import S3ArtifactStore, S3Settings
from cfr_v2.registry import ReleaseRegistry
from cfr_v2.runtime import RuntimeSettings, start_runtime, stop_runtime

from .bigquery import BigQueryCaseSource, BigQuerySettings
from .coordinator import IntakeCoordinator
from .redaction import EEUtilsMasker, Masker, ProfanityFilter
from .settings import IntakeSettings
from .source import CaseSource, RecordsCaseSource


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.intake", description=__doc__.splitlines()[0])
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--synthetic", type=int, metavar="N", help="admit N synthetic rows instead of querying BigQuery")
    args = parser.parse_args(argv)
    if args.synthetic is None and (args.start_date is None or args.end_date is None):
        parser.error("--start-date and --end-date are required unless --synthetic is given")

    settings = IntakeSettings.from_env()
    masker: Masker
    source: CaseSource
    if args.synthetic is not None:
        from .fakes import RegexMasker, synthetic_names, synthetic_rows

        masker, source = RegexMasker(synthetic_names()), RecordsCaseSource(synthetic_rows(args.synthetic))
    else:
        masker, source = EEUtilsMasker(), BigQueryCaseSource(BigQuerySettings.from_env())

    store = S3ArtifactStore(S3Settings.from_env())
    manifest = ReleaseRegistry(settings.registry_dir).load(settings.release_id)
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
    print(json.dumps({"manifest_ref": report.manifest_ref, **report.summary()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
