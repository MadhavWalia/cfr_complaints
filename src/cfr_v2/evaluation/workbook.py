"""The review workbook every stage shares (LDR-0042 §4): written by an export, read back by a score.

A workbook has one or more sheets of rows. Each sheet's columns are the stage's results, then the
reviewer's columns (blue headers, a dropdown when the answer is closed), then, on most stages, a
hidden ``Ref`` column that names the row exactly. Two more sheets hold what the reader needs:

- ``about``: what was exported, what every column means, and a ``Reviewer`` cell to fill in;
- ``_check`` (hidden): each row's key, a fingerprint of its result cells and the stored result it
  came from, so a score can tell an edited result from a reviewed one, and a re-run case from one
  unchanged since the export.

Text is always written as text (a claim starting with ``=`` never becomes a formula), and the
control characters Excel refuses are dropped (``cfr_v2.xlsx``). Rows are streamed: a release's
worth of claims never sits in memory, except the small secondary sheets (one row per case).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cfr_v2 import xlsx

REVIEW_FILL = "EDF7FF"  # Bright Blue 100: the console's Selected background, on the review headers
CHECK = "_check"
LISTS = "_lists"
ABOUT = "about"
REVIEWER = "Reviewer"

YN = ("Y", "N")
YPN = ("Y", "Partly", "N")

# How a typed answer is read back: case and spacing don't matter, nor the usual short forms.
_SYNONYMS = {
    "y": "Y",
    "yes": "Y",
    "n": "N",
    "no": "N",
    "p": "Partly",
    "partly": "Partly",
    "partial": "Partly",
    "partially": "Partly",
    "cant tell": "Can't tell",
    "can't tell": "Can't tell",
    "cannot tell": "Can't tell",
    "?": "Can't tell",
}


@dataclass(frozen=True)
class Column:
    header: str
    width: int = 20
    wrap: bool = False
    review: bool = False  # left empty for the reviewer
    choices: tuple[str, ...] = ()  # a dropdown; the answer must be one of them
    number: bool = False  # a whole number of 0 or more (a review count)
    hidden: bool = False
    note: str = ""  # what it means, for the about sheet
    optional: bool = False  # may be missing from an older workbook: read as unanswered
    choices_seen: str = ""  # also offer the values written in this column (before ``choices``)


@dataclass(frozen=True)
class Sheet:
    name: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]  # the headers that name a row; ("Ref",) where there is one

    @property
    def results(self) -> tuple[Column, ...]:
        """The columns an export fills, in order: every column but the review ones."""
        return tuple(c for c in self.columns if not c.review)

    @property
    def review(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.review)

    def column(self, header: str) -> Column:
        return next(c for c in self.columns if c.header == header)


@dataclass(frozen=True)
class Row:
    """One exported row: ``values`` fill the sheet's result columns, in order; ``stamp`` is the
    stored result it came from (an artifact ref), for the stale check."""

    sheet: str
    values: tuple[Any, ...]
    stamp: str = ""


def text(value: Any) -> str | None:
    """A cell as text, the same whether it was just written or read back: stripped, whole floats
    as integers, empty as ``None``."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    found = str(xlsx.clean(value)).strip()
    return found or None


def fingerprint(values: Iterable[Any]) -> str:
    body = json.dumps([text(v) or "" for v in values], ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _key(sheet: Sheet, by_header: Mapping[str, Any]) -> str:
    return json.dumps([text(by_header.get(h)) or "" for h in sheet.key])


# --- writing ------------------------------------------------------------------------------------


def _cell(ws: Any, value: Any, *, bold: bool = False, wrap: bool = False, fill: str | None = None) -> Any:
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill

    cell = WriteOnlyCell(ws, value=xlsx.clean(value))
    if isinstance(value, str):
        cell.data_type = "s"  # text, never a formula
    if bold:
        cell.font = Font(bold=True)
    if fill:
        cell.fill = PatternFill("solid", start_color=fill, end_color=fill)
    cell.alignment = Alignment(wrap_text=wrap, vertical="top")
    return cell


def write(
    path: Path,
    sheets: Sequence[Sheet],
    rows: Iterable[Row],
    about: Callable[[], Sequence[tuple[str, str]]],
) -> None:
    """Streams the first sheet's rows; buffers the rest (one row per case). ``about`` is called once
    the rows are written, so it can count them."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    book = Workbook(write_only=True)
    first, rest = sheets[0], sheets[1:]
    held: dict[str, list[Row]] = {s.name: [] for s in rest}
    checks: list[tuple[str, str, str, str]] = []  # sheet, key, fingerprint, stamp
    counts = {s.name: 0 for s in sheets}
    lists: dict[tuple[str, ...], int] = {}  # each distinct dropdown, its column in _lists
    seen: dict[tuple[str, str], dict[str, None]] = {}  # (sheet, header) -> values written, in order

    def open_sheet(sheet: Sheet) -> Any:
        ws = book.create_sheet(sheet.name)
        for number, column in enumerate(sheet.columns, start=1):
            letter = get_column_letter(number)
            ws.column_dimensions[letter].width = column.width
            if column.hidden:
                ws.column_dimensions[letter].hidden = True
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(sheet.columns))}1"
        ws.append([_cell(ws, c.header, bold=True, fill=REVIEW_FILL if c.review else None) for c in sheet.columns])
        return ws

    def put(ws: Any, sheet: Sheet, row: Row) -> None:
        results = sheet.results
        if len(row.values) != len(results):
            raise ValueError(f"{sheet.name}: {len(row.values)} values for {len(results)} result columns")
        given = dict(zip((c.header for c in results), row.values, strict=True))
        cells = [_cell(ws, None if c.review else given[c.header], wrap=c.wrap) for c in sheet.columns]
        ws.append(cells)
        counts[sheet.name] += 1
        for c in sheet.columns:
            if c.choices_seen and (value := text(given.get(c.choices_seen))):
                seen.setdefault((sheet.name, c.choices_seen), {})[value] = None
        compared = [given[c.header] for c in results if c.header not in sheet.key]
        checks.append((sheet.name, _key(sheet, given), fingerprint(compared), row.stamp))

    def validate(ws: Any, sheet: Sheet) -> None:
        end = max(counts[sheet.name] + 1, 2)
        for number, column in enumerate(sheet.columns, start=1):
            letter = get_column_letter(number)
            offered = column.choices
            if column.choices_seen:
                written = sorted(seen.get((sheet.name, column.choices_seen), {}))
                offered = (*(v for v in written if v not in column.choices), *column.choices)
            if offered:
                at = lists.setdefault(tuple(offered), len(lists) + 1)
                ref = get_column_letter(at)
                rule = DataValidation(
                    type="list", formula1=f"={LISTS}!${ref}$1:${ref}${len(offered)}", allow_blank=True
                )
            elif column.number:
                rule = DataValidation(type="whole", operator="greaterThanOrEqual", formula1="0", allow_blank=True)
            else:
                continue
            rule.add(f"{letter}2:{letter}{end}")
            ws.data_validations.append(rule)  # write-only sheets have no add_data_validation

    ws = open_sheet(first)
    for row in rows:
        if row.sheet == first.name:
            put(ws, first, row)
        else:
            held[row.sheet].append(row)
    validate(ws, first)
    for sheet in rest:
        ws = open_sheet(sheet)
        for row in held[sheet.name]:
            put(ws, sheet, row)
        validate(ws, sheet)

    notes = book.create_sheet(ABOUT)
    notes.column_dimensions["A"].width = 28
    notes.column_dimensions["B"].width = 90
    notes.append([_cell(notes, REVIEWER, bold=True), _cell(notes, None, fill=REVIEW_FILL)])
    for name, value in about():
        notes.append([_cell(notes, name, bold=True), _cell(notes, value, wrap=True)])
    for sheet in sheets:
        for column in sheet.columns:
            if column.note and not column.hidden:
                notes.append(
                    [_cell(notes, f"{sheet.name}: {column.header}", bold=True), _cell(notes, column.note, wrap=True)]
                )

    table = book.create_sheet(LISTS)
    table.sheet_state = "hidden"
    ordered = sorted(lists.items(), key=lambda item: item[1])
    for i in range(max((len(c) for c, _ in ordered), default=0)):
        table.append([_cell(table, c[i] if i < len(c) else None) for c, _ in ordered])

    check = book.create_sheet(CHECK)
    check.sheet_state = "hidden"
    check.append(["sheet", "key", "fingerprint", "stamp"])
    for entry in checks:
        check.append([_cell(check, v) for v in entry])
    book.save(path)


# --- reading ------------------------------------------------------------------------------------


@dataclass
class Read:
    """One row as the reviewer left it: every column's text, by header."""

    sheet: str
    line: int  # its row number in Excel
    values: dict[str, str | None]
    key: str
    stale: bool = False

    def __getitem__(self, header: str) -> str | None:
        return self.values.get(header)


@dataclass(frozen=True)
class Problem:
    sheet: str
    line: int
    reason: str  # what's wrong, in words; never the cell's text

    def json(self) -> dict[str, Any]:
        return {"sheet": self.sheet, "row": self.line, "reason": self.reason}


@dataclass
class Review:
    rows: dict[str, list[Read]]
    about: dict[str, str]
    problems: list[Problem] = field(default_factory=list)  # rows skipped, and why
    checked: bool = True  # False: no _check sheet (a workbook from before LDR-0042), so no edit check
    stamps: dict[tuple[str, str], str] = field(default_factory=dict)  # (sheet, key) -> stored result

    @property
    def reviewer(self) -> str | None:
        return self.about.get(REVIEWER) or None


class WorkbookError(ValueError):
    """The workbook isn't one this stage wrote, or can't be read."""


def answer(value: str | None, choices: Sequence[str]) -> str | None:
    """A typed answer as one of ``choices``: "yes", "Y " and "y" are all "Y". ``None`` when empty;
    raises ``ValueError`` when it is none of them."""
    if value is None:
        return None
    if value in choices:
        return value
    folded = " ".join(value.lower().replace("’", "'").split())
    for choice in choices:
        if folded == choice.lower():
            return choice
    synonym = _SYNONYMS.get(folded)
    if synonym in choices:
        return synonym
    raise ValueError("not one of the choices")


def read(path: Path, sheets: Sequence[Sheet]) -> Review:
    from openpyxl import load_workbook

    try:
        book = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is the same answer to the reviewer
        raise WorkbookError(f"{path.name} can't be read as a workbook ({type(exc).__name__})") from None
    try:
        missing = [s.name for s in sheets if s.name not in book.sheetnames]
        if missing:
            raise WorkbookError(
                f"{path.name} has no sheet {', '.join(repr(m) for m in missing)}: was it exported for this stage?"
            )
        about = {}
        if ABOUT in book.sheetnames:
            for name, value, *_ in book[ABOUT].iter_rows(values_only=True, max_col=2):
                if text(name):
                    about[text(name)] = text(value)
        fingerprints: dict[tuple[str, str], str] = {}
        stamps: dict[tuple[str, str], str] = {}
        checked = CHECK in book.sheetnames
        if checked:
            for i, row in enumerate(book[CHECK].iter_rows(values_only=True, max_col=4)):
                if i == 0 or not row or row[0] is None:
                    continue
                fingerprints[(str(row[0]), str(row[1]))] = str(row[2])
                stamps[(str(row[0]), str(row[1]))] = text(row[3]) or ""
        review = Review({}, about, checked=checked, stamps=stamps)
        for sheet in sheets:
            review.rows[sheet.name] = _read_sheet(book[sheet.name], sheet, fingerprints, review.problems, checked)
        return review
    finally:
        book.close()


def _read_sheet(ws: Any, sheet: Sheet, fingerprints: Mapping, problems: list[Problem], checked: bool) -> list[Read]:
    rows = ws.iter_rows(values_only=True)
    header = [text(h) for h in next(rows, ())]
    index = {h: i for i, h in enumerate(header) if h}
    absent = [c.header for c in sheet.columns if c.header not in index and not c.optional]
    if absent:
        raise WorkbookError(
            f"sheet {sheet.name!r} is missing the column(s) {', '.join(absent)}: export a fresh workbook"
            " (a hidden Ref column is easy to delete with its neighbours)"
        )
    found: list[Read] = []
    seen: set[str] = set()
    for line, raw in enumerate(rows, start=2):
        values = {h: text(raw[i]) if i < len(raw) else None for h, i in index.items()}
        if not any(values.get(c.header) for c in sheet.columns):
            continue  # an empty line
        key = _key(sheet, values)
        if key in seen:
            problems.append(Problem(sheet.name, line, "the same row appears twice; the first is used"))
            continue
        seen.add(key)
        if checked:
            expected = fingerprints.get((sheet.name, key))
            compared = [values.get(c.header) for c in sheet.results if c.header not in sheet.key]
            if expected is None:
                problems.append(Problem(sheet.name, line, "not a row this export wrote (its key was changed?)"))
                continue
            if fingerprint(compared) != expected:
                problems.append(
                    Problem(sheet.name, line, "a result cell was edited, so it no longer says what the stage said")
                )
                continue
        bad = False
        for column in sheet.review:
            value = values.get(column.header)
            if column.choices:
                try:
                    values[column.header] = answer(value, column.choices)
                except ValueError:
                    problems.append(Problem(sheet.name, line, f"{column.header}: not one of its choices"))
                    bad = True
            elif column.number and value is not None:
                try:
                    number = float(value)
                except ValueError:
                    number = -1.0
                if number < 0 or not number.is_integer():
                    problems.append(Problem(sheet.name, line, f"{column.header}: not a whole number"))
                    bad = True
                else:
                    values[column.header] = str(int(number))
        if not bad:
            found.append(Read(sheet.name, line, values, key))
    return found
