"""Export a stage's results for review, or score a reviewed workbook (LDR-0042).

    python -m cfr_v2.evaluation export <stage> --release R [--admitted-from D] [--admitted-to D]
        [--sample N [--seed S] | --case-no X ... | --cases-file F] --out review.xlsx
    python -m cfr_v2.evaluation score <stage> review.xlsx --out-dir DIR

``export`` reads stored results only, so it is safe while a worker runs. **Its workbook holds text
from cases (claims, reasoning, redacted transcript passages): keep it on this machine.**

``score`` reads the workbook, and the state database when it can reach it (to flag rows whose case
was re-run since the export). It writes ``<stage>_metrics.json`` (counts and rates, no case text)
and ``<stage>_metrics.xlsx`` (the same, plus the reviewers' notes: keep it local).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from cfr_v2.telemetry import get_logger

from . import STAGES
from .report import Rate, Report
from .source import MAX_CASES, MIN_CASES, Choice, Reader, current_refs, select_runs
from .stage import ExportContext
from .workbook import Review, WorkbookError, read, write

logger = get_logger("cfr_v2.evaluation")  # not __name__: run as -m, that is "__main__"


def _day(value: str) -> date:
    return date.fromisoformat(value)


def case_list(given: list[str], path: Path | None) -> tuple[str, ...]:
    """Case numbers from the command line and a .txt/.csv file: split on lines, commas, semicolons
    and spaces, repeats removed, order kept. A header such as ``case_no`` is skipped."""
    raw = list(given)
    if path is not None:
        raw += re.split(r"[\s,;]+", path.read_text(encoding="utf-8-sig"))
    found = [c.strip().strip('"') for c in raw]
    return tuple(dict.fromkeys(c for c in found if c and c.lower() not in ("case_no", "case no", "case")))


def export(args: argparse.Namespace) -> int:
    stage = STAGES[args.stage]
    case_nos = case_list(args.case_no or [], args.cases_file)
    if case_nos and not MIN_CASES <= len(case_nos) <= MAX_CASES:
        print(f"Listed cases: {len(case_nos)}; give between {MIN_CASES} and {MAX_CASES}.", file=sys.stderr)
        return 2
    choice = Choice(args.sample, args.seed, case_nos).with_seed()
    start = datetime.combine(args.admitted_from, time(), UTC) if args.admitted_from else None
    end = datetime.combine(args.admitted_to + timedelta(days=1), time(), UTC) if args.admitted_to else None

    import os

    from cfr_v2.artifacts import S3ArtifactStore, S3Settings
    from cfr_v2.runtime.settings import RuntimeSettings
    from cfr_v2.state import Database

    db = Database(RuntimeSettings.from_env().state_database_url, max_connections=1)
    store = S3ArtifactStore(S3Settings.from_env(os.environ))
    try:
        selection = select_runs(db, task=stage.task, release=args.release, start=start, end=end, choice=choice)
        window = " to ".join(str(d) for d in (args.admitted_from, args.admitted_to) if d) or "any day"
        print(
            f"{len(selection.runs):,} cases with a {stage.title} result under {args.release}, admitted {window}",
            flush=True,
        )
        if selection.not_found:
            print(
                f"{len(selection.not_found):,} listed cases have no such result: {', '.join(selection.not_found[:20])}",
                flush=True,
            )
        ctx = ExportContext(args.release, Path("registry"), store)
        reader = Reader(db, store)
        tally = {"cases": 0, "rows": 0, "skipped": []}
        versions: dict[str, int] = {}

        def rows():
            for run in selection.runs:
                try:
                    results = reader.results(run.case_run_id, stage.tasks)
                    if results is None:
                        raise LookupError("run not found")
                    produced = list(stage.rows(run, results, reader))
                except Exception as exc:  # noqa: BLE001 - one unreadable run is reported, not fatal
                    logger.warning("evaluation export: run %s skipped: %s", run.case_run_id, type(exc).__name__)
                    tally["skipped"].append(run.case_run_id)
                    continue
                for s in results.run.stages:
                    if s.task == stage.task and s.status == "SUCCEEDED":
                        versions[s.version] = versions.get(s.version, 0) + 1
                tally["cases"] += 1
                tally["rows"] += sum(1 for r in produced if r.sheet == stage.sheets()[0].name)
                yield from produced
                if tally["cases"] % 200 == 0:
                    print(f"[{tally['cases']}/{len(selection.runs)}] cases exported", flush=True)

        written = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

        def about() -> list[tuple[str, str]]:
            skipped = tally["skipped"]
            return [
                ("Stage", stage.title),
                ("Release", args.release),
                ("Admitted", window),
                ("Cases", f"{tally['cases']:,}: {choice.describe()}"),
                ("Seed", choice.seed or ""),
                ("Rows", f"{tally['rows']:,}"),
                ("Program version", ", ".join(f"{v} ({n:,})" for v, n in sorted(versions.items()))),
                ("Not found", ", ".join(selection.not_found)),
                ("Skipped", f"{len(skipped):,}" + (f": {', '.join(skipped[:20])}" if skipped else "")),
                ("Export", args.out.resolve().parent.parent.name if args.out.parent.name == "outputs" else ""),
                ("Written", written),
                ("Keep it local", "It holds text from cases: claims, reasoning and redacted transcript passages"),
            ]

        args.out.parent.mkdir(parents=True, exist_ok=True)
        sheets = stage.sheets(stage.choices(ctx, ()))
        write(args.out, sheets, rows(), about)
    finally:
        db.close()
    print(f"{tally['cases']:,} cases exported, {tally['rows']:,} rows, to {args.out.name}", flush=True)
    if tally["skipped"]:
        print(f"{len(tally['skipped']):,} cases skipped (results unreadable): {', '.join(tally['skipped'][:10])}")
    return 0


def flag_stale(review: Review, task: str) -> int:
    """Marks rows whose case run has a different result now than when exported (Q10: still scored,
    as exported). Needs the state database; without it, nothing is marked."""
    exported = {k: v.split(" ", 1) for k, v in review.stamps.items() if " " in v}
    if not exported:
        return 0
    try:
        from cfr_v2.runtime.settings import RuntimeSettings
        from cfr_v2.state import Database

        db = Database(RuntimeSettings.from_env().state_database_url, max_connections=1)
        try:
            now = current_refs(db, task, sorted({run for run, _ in exported.values()}))
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - scoring never needs the database
        logger.warning("evaluation score: no stale check (%s)", type(exc).__name__)
        return -1
    count = 0
    for sheet, rows in review.rows.items():
        for row in rows:
            run, ref = exported.get((sheet, row.key), ("", ""))
            if run and now.get(run, ref) != ref:
                row.stale = True
                count += 1
    return count


def write_metrics(report: Report, out: Path) -> None:
    """The report as a workbook to share: every table, then the reviewers' texts, then what was skipped."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    from cfr_v2 import xlsx

    def cell(value):
        if isinstance(value, Rate):
            return "" if value.value is None else f"{value.value:.1%} (n = {value.n})"
        return value

    book = Workbook()
    sheet = book.active
    sheet.title = "metrics"
    sheet.column_dimensions["A"].width = 48
    xlsx.append(sheet, [report.headline_label, cell(report.headline)])
    sheet.cell(row=1, column=1).font = Font(bold=True)
    for name, value in report.counts.items():
        xlsx.append(sheet, [name, value])
    for table in report.tables:
        xlsx.append(sheet, [])
        xlsx.append(sheet, [table.title])
        sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True)
        xlsx.append(sheet, list(table.columns))
        for row in table.rows:
            xlsx.append(sheet, [cell(c) for c in row])
        if table.note:
            xlsx.append(sheet, [table.note])
    for table in report.texts:
        texts = book.create_sheet(table.title[:31])
        xlsx.append(texts, list(table.columns))
        for row in table.rows:
            xlsx.append(texts, list(row))
    skipped = book.create_sheet("skipped")
    xlsx.append(skipped, ["sheet", "row", "reason"])
    for s in report.skipped:
        xlsx.append(skipped, [s["sheet"], s["row"], s["reason"]])
    book.save(out)


def score(args: argparse.Namespace) -> int:
    stage = STAGES[args.stage]
    try:
        review = read(args.workbook, stage.sheets())
    except WorkbookError as exc:
        print(f"Can't score it: {exc}", file=sys.stderr)
        return 2
    exported = review.about.get("Stage")
    if exported and exported != stage.title:
        print(f"Can't score it: it was exported for {exported}, not {stage.title}.", file=sys.stderr)
        return 2
    stale = flag_stale(review, stage.task)
    report = stage.score(review)
    report.skipped = [p.json() for p in review.problems]
    report.counts["skipped"] = len(review.problems)
    if stale >= 0:
        report.counts["stale (re-run since the export; scored as exported)"] = stale
    report.about = {
        "reviewer": review.reviewer,
        "release": review.about.get("Release"),
        "admitted": review.about.get("Admitted"),
        "cases": review.about.get("Cases"),
        "program version": review.about.get("Program version"),
        "export": review.about.get("Export"),
        "workbook": args.workbook.name,
        "edit check": review.checked,
        "scored": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"{stage.key}_metrics.json").write_text(json.dumps(report.json(), indent=2) + "\n")
    write_metrics(report, args.out_dir / f"{stage.key}_metrics.xlsx")
    for problem in review.problems[:10]:
        print(f"skipped: {problem.sheet} row {problem.line}: {problem.reason}")
    if not review.checked:
        print("This workbook has no _check sheet (exported before LDR-0042): edited results can't be detected.")
    print(report.line(), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.evaluation", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export", help="write a stage's results to a workbook for review")
    ex.add_argument("stage", choices=sorted(STAGES))
    ex.add_argument("--release", required=True)
    ex.add_argument("--admitted-from", type=_day)
    ex.add_argument("--admitted-to", type=_day)
    ex.add_argument("--sample", type=int, help=f"a random sample of N cases ({MIN_CASES}-{MAX_CASES})")
    ex.add_argument("--seed", help="the same seed gives the same sample")
    ex.add_argument("--case-no", action="append", help="a case to export (repeat)")
    ex.add_argument("--cases-file", type=Path, help="a .txt or .csv of case numbers")
    ex.add_argument("--out", type=Path, required=True)
    sc = sub.add_parser("score", help="compute a stage's metrics from a reviewed workbook")
    sc.add_argument("stage", choices=sorted(STAGES))
    sc.add_argument("workbook", type=Path)
    sc.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "export":
        if args.admitted_from and args.admitted_to and args.admitted_to < args.admitted_from:
            parser.error("--admitted-to is before --admitted-from")
        if args.sample is not None and not MIN_CASES <= args.sample <= MAX_CASES:
            parser.error(f"--sample must be between {MIN_CASES} and {MAX_CASES}")
        if args.sample and (args.case_no or args.cases_file):
            parser.error("give --sample or a list of cases, not both")
        return export(args)
    return score(args)


if __name__ == "__main__":
    sys.exit(main())
