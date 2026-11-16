"""The routing results of finished case runs, as an Excel workbook (LDR-0033).

    python -m cfr_v2.console.export --release cfr-v2-phase-e --out routing.xlsx
    python -m cfr_v2.console.export --release cfr-v2-phase-e --admitted-from 2026-11-15 --out routing.xlsx

One row per claim and vertical: Case no, Claim, Primary (Y/N), Vertical, Reasoning, Sections, then
three empty columns for a reviewer: Correct? (Y/N), Correct vertical (if not), Reason it's wrong. The
first two are dropdowns: Y/N, and the release's verticals with UNMAPPED and NO_ELIGIBLE_VERTICAL.
Only runs at Plan ready under the release, admitted in the window when one is given. A claim C04
didn't route shows its outcome as the vertical. A second sheet records what was exported.

Stage results are read as the case page reads them (``cases.read_result``): only results the run's
own stages wrote, and only INTERNAL ones, never transcripts. Read-only, so it is safe while a worker
runs. **The workbook holds claim statements and reasoning taken from cases: keep it on this machine.**
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from cfr_v2.telemetry import get_logger

from . import cases

logger = get_logger("cfr_v2.console.export")  # not __name__: run as -m, that is "__main__"

RESULT_HEADERS = ("Case no", "Claim", "Primary (Y/N)", "Vertical", "Reasoning", "Sections")
REVIEW_HEADERS = ("Correct? (Y/N)", "Correct vertical (if not)", "Reason it's wrong")  # left empty for a reviewer
HEADERS = (*RESULT_HEADERS, *REVIEW_HEADERS)
WIDTHS = (16, 60, 13, 28, 70, 60, 14, 28, 50)
WRAPPED = {"Claim", "Reasoning", "Sections", "Reason it's wrong"}
OUTCOMES = ("UNMAPPED", "NO_ELIGIBLE_VERTICAL")
REVIEW_FILL = "EDF7FF"  # the review columns' headers: Bright Blue 100, as the console marks a selection
STAGES = ("c02.pass2", "c03.primary_selection", "c04.routing")  # the only results the export reads
# Plan-ready runs of a release, admitted in [start, end), oldest first.
RUNS_SQL = (
    "SELECT r.case_run_id, c.source_case_id FROM case_run r"
    ' JOIN "case" c ON c.case_id = r.case_id'
    " JOIN release_manifest rm ON rm.manifest_ref = r.release_manifest_ref"
    " JOIN LATERAL (SELECT state FROM case_run_event e WHERE e.case_run_id = r.case_run_id"
    " ORDER BY e.event_id DESC LIMIT 1) latest ON true"
    " WHERE latest.state = 'PLAN_READY' AND rm.release_id = %(release)s"
    " AND (%(start)s::timestamptz IS NULL OR r.created_at >= %(start)s)"
    " AND (%(end)s::timestamptz IS NULL OR r.created_at < %(end)s)"
    " ORDER BY r.created_at, r.case_run_id"
)


@dataclass
class Export:
    runs: int = 0
    rows: int = 0
    skipped: list[str] = field(default_factory=list)  # case runs whose results couldn't be read


def _sections(procedures: Iterable[Mapping[str, Any]]) -> str:
    lines = []
    for p in procedures:
        start, end = p.get("page_start"), p.get("page_end")
        pages = f"p. {start}" if start == end or end is None else f"p. {start}–{end}"
        lines.append(f"{p.get('file_name', '')} · {p.get('section_title', '')} ({pages})")
    return "\n".join(lines)


def claim_rows(
    case_no: str, pass2: Mapping[str, Any] | None, selection: Mapping[str, Any] | None, routing: Mapping[str, Any]
) -> Iterator[tuple[str, ...]]:
    """One row per claim and vertical it was routed to: usually one vertical per claim."""
    claims = {c.get("claim_id"): c for c in (pass2 or {}).get("claims", []) if isinstance(c, Mapping)}
    primary = (selection or {}).get("claim_id") if (selection or {}).get("status") == "SELECTED" else None
    for claim in routing.get("claims", []):
        claim_id = claim.get("claim_id")
        statement = (claims.get(claim_id) or {}).get("statement") or claim_id or ""
        is_primary = "Y" if claim_id is not None and claim_id == primary else "N"
        candidates = claim.get("candidates") or []
        if claim.get("outcome") == "ROUTED" and candidates:
            for candidate in candidates:
                reasoning = candidate.get("rationale") or claim.get("rationale") or ""
                yield (
                    case_no,
                    statement,
                    is_primary,
                    candidate.get("vertical_id", ""),
                    reasoning,
                    _sections(candidate.get("procedures") or ()),
                )
        else:
            yield (case_no, statement, is_primary, claim.get("outcome", ""), claim.get("rationale") or "", "")


def read_results(db, store, case_run_id: str) -> dict[str, Any]:
    """The latest successful result of each stage the export needs, as the case page reads them."""
    run = cases.read_case(db, case_run_id)
    found: dict[str, Any] = {}
    for stage in run.stages if run else ():
        if stage.task in STAGES and stage.status == "SUCCEEDED" and stage.refs:
            found[stage.task] = cases.parse_result(cases.read_result(db, store, run, stage.refs[0]))
    return found


def rows_for(db, store, runs: Iterable[tuple[str, str]], report: Export) -> Iterator[tuple[str, ...]]:
    for case_run_id, case_no in runs:
        try:
            results = read_results(db, store, case_run_id)
        except Exception as exc:  # noqa: BLE001 - one unreadable run is reported, not fatal
            logger.warning("export: run %s skipped: %s", case_run_id, type(exc).__name__)
            report.skipped.append(case_run_id)
            continue
        routing = results.get("c04.routing")
        if not isinstance(routing, Mapping):
            report.skipped.append(case_run_id)
            continue
        report.runs += 1
        for row in claim_rows(case_no, results.get("c02.pass2"), results.get("c03.primary_selection"), routing):
            report.rows += 1
            yield row
        if report.runs % 500 == 0:
            print(f"[{report.runs}] case runs exported, {report.rows} rows", flush=True)


def _text(sheet: Any, value: Any, *, bold: bool = False, wrap: bool = False, fill: str | None = None) -> Any:
    """A cell that is always text: a claim starting with '=' must not become a formula, and control
    characters a call's text can carry (which Excel can't store; openpyxl refuses the whole cell)
    are dropped. They print nothing, so the text reads the same."""
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill

    from cfr_v2 import xlsx

    cell = WriteOnlyCell(sheet, value=xlsx.clean(value))
    if isinstance(value, str):
        cell.data_type = "s"
    if bold:
        cell.font = Font(bold=True)
    if fill:
        cell.fill = PatternFill("solid", start_color=fill, end_color=fill)
    if wrap:
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    else:
        cell.alignment = Alignment(vertical="top")
    return cell


def write_workbook(
    path: Path,
    rows: Iterable[Sequence[str]],
    about: Callable[[], Sequence[tuple[str, str]]],
    verticals: Sequence[str] = (),
) -> None:
    """Streams the rows: a full release's worth of claims never sits in memory. ``about`` is called
    once they're written, so it can count them. ``verticals`` fill the Correct vertical dropdown;
    without them, the verticals the rows were routed to do."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    book = Workbook(write_only=True)
    sheet = book.create_sheet("routing")
    for number, width in enumerate(WIDTHS, start=1):
        sheet.column_dimensions[get_column_letter(number)].width = width
    sheet.freeze_panes = "A2"
    last = get_column_letter(len(HEADERS))
    sheet.auto_filter.ref = f"A1:{last}1"
    sheet.append([_text(sheet, h, bold=True, fill=REVIEW_FILL if h in REVIEW_HEADERS else None) for h in HEADERS])
    seen: set[str] = set()
    count = 0
    for row in rows:
        count += 1
        seen.add(row[RESULT_HEADERS.index("Vertical")])
        cells = [_text(sheet, v, wrap=h in WRAPPED) for h, v in zip(RESULT_HEADERS, row, strict=True)]
        cells += [_text(sheet, None, wrap=h in WRAPPED) for h in REVIEW_HEADERS]  # for the reviewer
        sheet.append(cells)
    # The dropdowns, added once the rows are written: a list in a hidden sheet, as Excel caps an
    # inline list at 255 characters.
    choices = [*dict.fromkeys(v for v in (verticals or sorted(seen - set(OUTCOMES))) if v), *OUTCOMES]
    lists = book.create_sheet("_lists")
    lists.sheet_state = "hidden"
    for choice in choices:
        lists.append([_text(lists, choice)])
    end = max(count + 1, 2)
    correct_col = get_column_letter(HEADERS.index("Correct? (Y/N)") + 1)
    vertical_col = get_column_letter(HEADERS.index("Correct vertical (if not)") + 1)
    correct = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    correct.add(f"{correct_col}2:{correct_col}{end}")
    should = DataValidation(type="list", formula1=f"=_lists!$A$1:$A${len(choices)}", allow_blank=True)
    should.add(f"{vertical_col}2:{vertical_col}{end}")
    sheet.data_validations.append(correct)  # write-only sheets have no add_data_validation
    sheet.data_validations.append(should)
    notes = book.create_sheet("about")
    notes.column_dimensions["A"].width = 24
    notes.column_dimensions["B"].width = 90
    for name, value in about():
        notes.append([_text(notes, name, bold=True), _text(notes, value, wrap=True)])
    book.save(path)


def release_verticals(registry: Path, release: str, store: Any) -> list[str]:
    """The vertical ids in the knowledge document the release pins, as the router reads them; none
    when the file or the document can't be read."""
    from cfr_v2.contracts import AuthorizationContext, Classification
    from cfr_v2.registry import ReleaseRegistry
    from cfr_v2.routing.verticals import VerticalCatalog

    try:
        manifest = ReleaseRegistry(registry).load(release)
        if manifest.routing_verticals is None:
            return []
        reader = AuthorizationContext(principal="console", purpose="routing export: the vertical list")
        raw = store.get(manifest.routing_verticals, classification=Classification.INTERNAL, authorization=reader)
        return list(VerticalCatalog.parse(raw.decode("utf-8")).ids)
    except Exception as exc:  # noqa: BLE001 - the dropdown falls back to the verticals in the rows
        logger.warning("export: the release's verticals couldn't be read (%s)", type(exc).__name__)
        return []


def _day(value: str) -> date:
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.console.export", description=__doc__.splitlines()[0])
    parser.add_argument("--release", required=True, help="the release id, e.g. cfr-v2-phase-e")
    parser.add_argument("--admitted-from", type=_day, help="first admission day, YYYY-MM-DD (inclusive)")
    parser.add_argument("--admitted-to", type=_day, help="last admission day, YYYY-MM-DD (inclusive)")
    parser.add_argument("--out", type=Path, required=True, help="the .xlsx to write")
    args = parser.parse_args(argv)
    if args.admitted_from and args.admitted_to and args.admitted_to < args.admitted_from:
        parser.error("--admitted-to is before --admitted-from")
    start = datetime.combine(args.admitted_from, time(), UTC) if args.admitted_from else None
    end = datetime.combine(args.admitted_to + timedelta(days=1), time(), UTC) if args.admitted_to else None

    import os

    from cfr_v2.artifacts import S3ArtifactStore, S3Settings
    from cfr_v2.runtime.settings import RuntimeSettings
    from cfr_v2.state import Database

    db = Database(RuntimeSettings.from_env().state_database_url, max_connections=1)
    store = S3ArtifactStore(S3Settings.from_env(os.environ))
    report = Export()
    window = " to ".join(str(d) for d in (args.admitted_from, args.admitted_to) if d) or "any day"
    written = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    def about() -> list[tuple[str, str]]:
        return [
            ("Release", args.release),
            ("Admitted", window),
            ("Case runs", f"{report.runs:,} at Plan ready"),
            ("Rows", f"{report.rows:,}: one per claim and vertical"),
            ("Skipped", f"{len(report.skipped):,}" + (f": {', '.join(report.skipped[:20])}" if report.skipped else "")),
            ("Written", written),
            (
                "Primary (Y/N)",
                "Y for the claim C03 selected; N for the rest, and for every claim when C03 found it ambiguous",
            ),
            ("Vertical", "Where C04 routed the claim, or UNMAPPED / NO_ELIGIBLE_VERTICAL when it didn't"),
            ("Sections", "The procedure sections the decision relied on, one per line"),
            ("Correct? (Y/N)", "For the reviewer: is the vertical right for this claim?"),
            (
                "Correct vertical (if not)",
                "For the reviewer: the right one, from the dropdown (" + vertical_source[0] + ")",
            ),
            ("Reason it's wrong", "For the reviewer: why the routing is wrong, in a sentence or two"),
            ("Keep it local", "It holds claim statements and reasoning taken from cases"),
        ]

    verticals = release_verticals(Path("registry"), args.release, store)
    vertical_source = [
        "the release's verticals"
        if verticals
        else "the verticals these claims were routed to: the release's couldn't be read"
    ]
    try:
        with db.transaction() as tx:
            runs = [(r[0], r[1]) for r in tx.all(RUNS_SQL, {"release": args.release, "start": start, "end": end})]
        print(f"{len(runs):,} case runs at Plan ready under {args.release}, admitted {window}", flush=True)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        write_workbook(args.out, rows_for(db, store, runs, report), about, verticals)
    finally:
        db.close()
    print(f"{report.runs:,} case runs exported, {report.rows:,} rows, to {args.out.name}", flush=True)
    if report.skipped:
        print(f"{len(report.skipped):,} case runs skipped (results unreadable): {', '.join(report.skipped[:10])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
