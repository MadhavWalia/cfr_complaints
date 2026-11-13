"""Gold-set workbooks for the prompts after Pass 1 (LDR-0027).

A template has a **read me** sheet, **context** sheets the labeller reads but can't edit (the
frozen inputs the prompt will see: Pass 1's items, the claims, the claim and its sections),
**answer** sheets with dropdowns for every fixed value, and a hidden ``_frozen`` sheet that
records which committed result each case's context came from, so a workbook filled against an
older result is caught on upload. Dropdown values live on a hidden ``_lists`` sheet: Excel caps
an inline list at 255 characters.

Workbooks hold case text: keep them on this machine and delete them once reviewed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROWS = 2000  # rows of an answer sheet that get dropdowns
KEEP_LOCAL = (
    "This workbook holds case text. Keep it on this machine, upload it only to the CFR console on this "
    "machine, and delete it once the optimization run has been reviewed."
)


@dataclass
class Sheet:
    name: str
    columns: Sequence[str]
    rows: list[Sequence[Any]] = field(default_factory=list)
    editable: frozenset[str] = frozenset()  # columns the labeller fills; all others are locked
    choices: Mapping[str, Sequence[str]] = field(default_factory=dict)  # column -> dropdown values
    wide: frozenset[str] = frozenset()  # long text columns
    free_rows: bool = False  # the labeller adds rows (claims): the sheet isn't locked


def write(path: Path, *, title: str, steps: Sequence[str], sheets: Sequence[Sheet], frozen: Mapping[str, str]) -> None:
    """``frozen``: case number -> the committed result's ref its context came from."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Protection
    from openpyxl.worksheet.datavalidation import DataValidation

    book = Workbook()
    readme = book.active
    readme.title = "read me"
    readme.column_dimensions["A"].width = 110
    for line in (title, "", KEEP_LOCAL, "", *steps):
        readme.append([line])
    readme["A1"].font = Font(bold=True, size=13)
    readme["A3"].font = Font(bold=True)
    for row in readme.iter_rows():
        row[0].alignment = Alignment(wrap_text=True, vertical="top")

    lists = book.create_sheet("_lists")
    lists.sheet_state = "hidden"
    list_column = 0
    locked_fill = PatternFill("solid", fgColor="ECEDEE")  # Gray 02: read, don't edit
    for spec in sheets:
        sheet = book.create_sheet(spec.name)
        sheet.append(list(spec.columns))
        for row in spec.rows:
            sheet.append(list(row))
        last = max(len(spec.rows) + 1, 2)
        span = ROWS if spec.free_rows else last
        for i, column in enumerate(spec.columns):
            letter = _letter(i)
            sheet.column_dimensions[letter].width = 70 if column in spec.wide else 18
            if column in spec.choices:
                list_column += 1
                values = list(spec.choices[column])
                list_letter = _letter(list_column - 1)
                for r, value in enumerate(values, start=1):
                    lists[f"{list_letter}{r}"] = value
                rule = DataValidation(
                    type="list",
                    formula1=f"=_lists!${list_letter}$1:${list_letter}${max(len(values), 1)}",
                    allow_blank=True,
                )
                rule.error, rule.showErrorMessage = "Choose a value from the list", True
                sheet.add_data_validation(rule)
                rule.add(f"{letter}2:{letter}{span}")
            for r in range(2, span + 1):
                cell = sheet[f"{letter}{r}"]
                if column in spec.editable or spec.free_rows:
                    cell.protection = Protection(locked=False)
                else:
                    cell.fill = locked_fill
                if column in spec.wide:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        if not spec.free_rows:
            sheet.protection.sheet = True  # context can't be edited by mistake; answer columns stay open

    meta = book.create_sheet("_frozen")
    meta.sheet_state = "hidden"
    meta.append(["case_no", "context_ref"])
    for case_no, ref in frozen.items():
        meta.append([case_no, ref])
    book.save(path)


def _letter(index: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(index + 1)


class WorkbookError(ValueError):
    pass


def read(path: Path, sheet: str, columns: Sequence[str]) -> list[tuple[int, dict[str, str | None]]]:
    """``(row number, {column: text or None})`` for each non-empty row; ``WorkbookError`` naming
    the sheet when it or a column is missing. Cell text is returned, never printed."""
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    if sheet not in book.sheetnames:
        raise WorkbookError(f"{path.name}: no sheet {sheet!r}; was this template made for this prompt?")
    rows = book[sheet].iter_rows(values_only=True)
    header = [str(c).strip() if c is not None else "" for c in next(rows, ())]
    missing = [c for c in columns if c not in header]
    if missing:
        raise WorkbookError(f"{path.name}: sheet {sheet!r} has no column {', '.join(missing)}")
    out = []
    for number, row in enumerate(rows, start=2):
        record = {c: _text(row[header.index(c)]) if header.index(c) < len(row) else None for c in columns}
        if any(v is not None for v in record.values()):
            out.append((number, record))
    return out


def frozen(path: Path) -> dict[str, str]:
    """The ``_frozen`` sheet: case number -> the result ref its context was frozen from."""
    try:
        return {r["case_no"]: r["context_ref"] for _, r in read(path, "_frozen", ("case_no", "context_ref"))}
    except WorkbookError:
        return {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    text = str(value).strip()
    return text or None


def numbers(text: str | None) -> list[int] | None:
    """``"1, 3"`` -> ``[1, 3]``; None when not a list of whole numbers."""
    if text is None:
        return []
    try:
        return [int(part) for part in text.replace(";", ",").split(",") if part.strip()]
    except ValueError:
        return None


__all__ = ["KEEP_LOCAL", "Sheet", "WorkbookError", "frozen", "numbers", "read", "write"]
