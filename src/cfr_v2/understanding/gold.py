"""The Pass 1 gold set: the business team's workbooks (lld/guides/gold-set-allegations.md).

One workbook per labeller with five sheets (``cases``, ``allegations``, ``details``,
``requests``, ``agent_statements``). ``read_gold`` validates every row and reports the
sheet and row of each problem; ``write_template`` produces an empty workbook with the
case list filled in and dropdowns for every fixed value, so entry errors are caught
while typing.

Gold workbooks hold transcript quotes: treat them as RESTRICTED customer data. This module
never logs or prints their text.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLEGATION_TYPES = {"claim": "CLAIM", "impact": "IMPACT"}
ABOUT = ("agent", "customer", "system", "third_party", "mixed")
DETAIL_TYPES = ("when", "amount", "merchant_or_product", "earlier_contact", "other")
REQUEST_TYPES = (
    "refund_or_credit",
    "fee_reversal",
    "cancel_or_close",
    "replace_card",
    "explanation",
    "escalation",
    "callback",
    "other",
)
STATEMENT_TYPES = {"fact": "FACT", "action": "ACTION", "commitment": "COMMITMENT", "advice": "ADVICE"}
SPEAKER_LABELS = ("reliable", "partly", "unreliable")

SHEETS: dict[str, tuple[str, ...]] = {
    "cases": (
        "case_no",
        "labeller",
        "allegation_count",
        "request_count",
        "statement_count",
        "speaker_labels",
        "could_not_label",
        "primary_allegation_no",
        "primary_unclear",
        "notes",
    ),
    "allegations": (
        "case_no",
        "allegation_no",
        "allegation",
        "type",
        "about",
        "quote",
        "quote_2",
        "ambiguous",
        "ambiguity_note",
    ),
    "details": ("case_no", "allegation_no", "detail_type", "detail", "quote"),
    "requests": (
        "case_no",
        "request_no",
        "request",
        "request_type",
        "quote",
        "for_allegations",
        "ambiguous",
        "ambiguity_note",
    ),
    "agent_statements": (
        "case_no",
        "statement_no",
        "statement",
        "type",
        "quote",
        "quote_2",
        "responds_to_allegations",
        "responds_to_requests",
        "ambiguous",
        "ambiguity_note",
    ),
}


# Added with C03 (LDR-0018); workbooks made before then don't have them.
OPTIONAL_COLUMNS = frozenset({("cases", "primary_allegation_no"), ("cases", "primary_unclear")})


class GoldError(ValueError):
    """A workbook problem, with its sheet and row; never the cell's text."""


@dataclass(frozen=True)
class GoldItem:
    number: int  # allegation_no / request_no / statement_no, 1-based within the case
    kind: str  # CLAIM / IMPACT, a request type, or FACT / ACTION / COMMITMENT / ADVICE
    quotes: tuple[str, ...]
    row: int  # sheet row, for reporting problems back to the labeller
    about: str | None = None  # allegations only
    allegations: frozenset[int] = frozenset()  # links (requests, statements)
    requests: frozenset[int] = frozenset()  # links (statements)
    ambiguous: bool = False


@dataclass(frozen=True)
class GoldDetail:
    allegation: int
    kind: str
    quote: str
    row: int


@dataclass
class GoldCase:
    case_no: str
    labeller: str
    speaker_labels: str
    could_not_label: bool
    source: str  # workbook file name, for reports
    row: int = 0  # row on the cases sheet
    primary: int | None = None  # primary_allegation_no: the main complaint (LDR-0018)
    primary_unclear: bool | None = None
    allegations: list[GoldItem] = field(default_factory=list)
    details: list[GoldDetail] = field(default_factory=list)
    requests: list[GoldItem] = field(default_factory=list)
    statements: list[GoldItem] = field(default_factory=list)


def _cell(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def _rows(book, sheet: str, source: str) -> list[tuple[int, dict[str, str | None]]]:
    if sheet not in book.sheetnames:
        raise GoldError(f"{source}: missing sheet {sheet!r}")
    rows = book[sheet].iter_rows(values_only=True)
    header = [_cell(h) for h in next(rows, ())]
    missing = [c for c in SHEETS[sheet] if c not in header and (sheet, c) not in OPTIONAL_COLUMNS]
    if missing:
        raise GoldError(f"{source}: sheet {sheet!r} is missing columns {missing}")
    out = []
    for number, row in enumerate(rows, start=2):
        record = {name: _cell(row[i]) if i < len(row) else None for i, name in enumerate(header) if name}
        if any(record.get(c) for c in SHEETS[sheet]):
            out.append((number, record))
    return out


class _Reader:
    def __init__(self, source: str) -> None:
        self.source = source

    def fail(self, sheet: str, row: int, problem: str) -> GoldError:
        return GoldError(f"{self.source}: {sheet} row {row}: {problem}")

    def required(self, sheet: str, row: int, record: dict, column: str) -> str:
        value = record.get(column)
        if not value:
            raise self.fail(sheet, row, f"{column} is empty")
        return value

    def number(self, sheet: str, row: int, record: dict, column: str) -> int:
        value = self.required(sheet, row, record, column)
        if not value.isdigit() or int(value) < 1:
            raise self.fail(sheet, row, f"{column} must be a whole number from 1")
        return int(value)

    def choice(self, sheet: str, row: int, record: dict, column: str, allowed) -> str:
        value = self.required(sheet, row, record, column).lower()
        options = allowed if isinstance(allowed, dict) else {a: a for a in allowed}
        if value not in options:
            raise self.fail(sheet, row, f"{column} must be one of {', '.join(options)}")
        return options[value]

    def yes(self, sheet: str, row: int, record: dict, column: str) -> bool:
        return self.choice(sheet, row, record, column, {"y": True, "n": False})

    def numbers(self, sheet: str, row: int, record: dict, column: str) -> frozenset[int]:
        value = record.get(column)
        if not value:
            return frozenset()
        parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
        if not all(p.isdigit() for p in parts):
            raise self.fail(sheet, row, f"{column} must be numbers separated by commas")
        return frozenset(int(p) for p in parts)

    def quotes(self, sheet: str, row: int, record: dict, *columns: str) -> tuple[str, ...]:
        found = tuple(q for q in (record.get(c) for c in columns) if q)
        if not found:
            raise self.fail(sheet, row, f"{columns[0]} is empty")
        return found


def read_gold(path: Path) -> list[GoldCase]:
    """All labelled cases in one workbook. Raises ``GoldError`` naming the sheet and row of the first problem."""
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    r = _Reader(path.name)
    cases: dict[str, GoldCase] = {}
    for row, rec in _rows(book, "cases", path.name):
        case_no = r.required("cases", row, rec, "case_no")
        if case_no in cases:
            raise r.fail("cases", row, "case_no appears twice")
        cases[case_no] = GoldCase(
            case_no=case_no,
            labeller=r.required("cases", row, rec, "labeller"),
            speaker_labels=r.choice("cases", row, rec, "speaker_labels", SPEAKER_LABELS),
            could_not_label=r.yes("cases", row, rec, "could_not_label"),
            source=path.name,
            row=row,
            primary=r.number("cases", row, rec, "primary_allegation_no") if rec.get("primary_allegation_no") else None,
            primary_unclear=r.yes("cases", row, rec, "primary_unclear") if rec.get("primary_unclear") else None,
        )

    def case_for(sheet: str, row: int, rec: dict) -> GoldCase:
        case_no = r.required(sheet, row, rec, "case_no")
        if case_no not in cases:
            raise r.fail(sheet, row, "case_no is not on the cases sheet")
        return cases[case_no]

    def unique(items: list[GoldItem], item: GoldItem, sheet: str, row: int) -> None:
        if any(i.number == item.number for i in items):
            raise r.fail(sheet, row, "number appears twice for this case")
        items.append(item)

    for row, rec in _rows(book, "allegations", path.name):
        case = case_for("allegations", row, rec)
        r.required("allegations", row, rec, "allegation")
        unique(
            case.allegations,
            GoldItem(
                number=r.number("allegations", row, rec, "allegation_no"),
                kind=r.choice("allegations", row, rec, "type", ALLEGATION_TYPES),
                about=r.choice("allegations", row, rec, "about", ABOUT),
                quotes=r.quotes("allegations", row, rec, "quote", "quote_2"),
                ambiguous=r.yes("allegations", row, rec, "ambiguous"),
                row=row,
            ),
            "allegations",
            row,
        )
    for row, rec in _rows(book, "details", path.name):
        case = case_for("details", row, rec)
        r.required("details", row, rec, "detail")
        case.details.append(
            GoldDetail(
                allegation=r.number("details", row, rec, "allegation_no"),
                kind=r.choice("details", row, rec, "detail_type", DETAIL_TYPES),
                quote=r.required("details", row, rec, "quote"),
                row=row,
            )
        )
    for row, rec in _rows(book, "requests", path.name):
        case = case_for("requests", row, rec)
        r.required("requests", row, rec, "request")
        unique(
            case.requests,
            GoldItem(
                number=r.number("requests", row, rec, "request_no"),
                kind=r.choice("requests", row, rec, "request_type", REQUEST_TYPES),
                quotes=r.quotes("requests", row, rec, "quote"),
                allegations=r.numbers("requests", row, rec, "for_allegations"),
                ambiguous=r.yes("requests", row, rec, "ambiguous"),
                row=row,
            ),
            "requests",
            row,
        )
    for row, rec in _rows(book, "agent_statements", path.name):
        case = case_for("agent_statements", row, rec)
        r.required("agent_statements", row, rec, "statement")
        unique(
            case.statements,
            GoldItem(
                number=r.number("agent_statements", row, rec, "statement_no"),
                kind=r.choice("agent_statements", row, rec, "type", STATEMENT_TYPES),
                quotes=r.quotes("agent_statements", row, rec, "quote", "quote_2"),
                allegations=r.numbers("agent_statements", row, rec, "responds_to_allegations"),
                requests=r.numbers("agent_statements", row, rec, "responds_to_requests"),
                ambiguous=r.yes("agent_statements", row, rec, "ambiguous"),
                row=row,
            ),
            "agent_statements",
            row,
        )

    for case in cases.values():
        numbers = {a.number for a in case.allegations}
        for d in case.details:
            if d.allegation not in numbers:
                raise r.fail("details", d.row, "allegation_no is not an allegation of this case")
        request_numbers = {q.number for q in case.requests}
        for item in (*case.requests, *case.statements):
            sheet = "requests" if item in case.requests else "agent_statements"
            if not item.allegations <= numbers or not item.requests <= request_numbers:
                raise r.fail(sheet, item.row, "links to an allegation or request that doesn't exist")
        if case.primary is not None:
            if case.primary not in numbers:
                raise r.fail("cases", case.row, "primary_allegation_no is not an allegation of this case")
            if case.primary_unclear is None:
                raise r.fail("cases", case.row, "primary_unclear is empty")
        if case.could_not_label and (case.allegations or case.requests or case.statements):
            raise GoldError(f"{path.name}: case {case.case_no} is could_not_label = Y but has rows")
    return list(cases.values())


def write_template(case_nos: list[str], path: Path) -> None:
    """An empty gold workbook: the case list on ``cases``, headers and dropdowns everywhere."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.worksheet.datavalidation import DataValidation

    rows = 1000
    book = Workbook()
    book.remove(book.active)
    lists = {
        ("cases", "speaker_labels"): SPEAKER_LABELS,
        ("cases", "could_not_label"): ("Y", "N"),
        ("cases", "primary_unclear"): ("Y", "N"),
        ("allegations", "type"): ("Claim", "Impact"),
        ("allegations", "about"): ABOUT,
        ("allegations", "ambiguous"): ("Y", "N"),
        ("details", "detail_type"): DETAIL_TYPES,
        ("requests", "request_type"): REQUEST_TYPES,
        ("requests", "ambiguous"): ("Y", "N"),
        ("agent_statements", "type"): ("Fact", "Action", "Commitment", "Advice"),
        ("agent_statements", "ambiguous"): ("Y", "N"),
    }
    case_list = f"cases!$A$2:$A${max(len(case_nos), 1) + 1}"
    for name, columns in SHEETS.items():
        sheet = book.create_sheet(name)
        sheet.append(list(columns))
        if name == "cases":
            for case_no in case_nos:
                sheet.append([case_no])
        for i, column in enumerate(columns):
            letter = chr(ord("A") + i)
            sheet.column_dimensions[letter].width = (
                60 if column in ("allegation", "request", "statement") or "quote" in column else 18
            )
            options = lists.get((name, column))
            formula = (
                f'"{",".join(options)}"'
                if options
                else (f"={case_list}" if column == "case_no" and name != "cases" else None)
            )
            if formula:
                validation = DataValidation(type="list", formula1=formula, allow_blank=True)
                validation.error = "Choose a value from the list"
                validation.showErrorMessage = True
                sheet.add_data_validation(validation)
                validation.add(f"{letter}2:{letter}{rows}")
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.freeze_panes = "A2"
    book.save(path)
