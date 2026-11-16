"""Text into Excel safely, for every workbook the pipeline writes (LDR-0035).

The workbooks hold text from calls: transcripts, allegations, claims, reasoning. Two things in such
text break a workbook:

- **Control characters** (codes 0–8, 11–12, 14–31) that transcription or copying leave in the text.
  Excel can't store them, and openpyxl refuses the whole cell: on 2026-11-16 one vertical tab in a
  claim's reasoning stopped an export of 3k cases. They print nothing, so dropping them keeps the
  text as it reads. Tab, newline and carriage return are kept.
- **A leading "="**, which openpyxl writes as a formula. Text is always written as text here.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def clean(value: Any) -> Any:
    """``value`` as Excel can store it: text without the control characters Excel refuses.
    Anything that isn't text is returned unchanged."""
    if not isinstance(value, str):
        return value
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    return ILLEGAL_CHARACTERS_RE.sub("", value)


def append(sheet: Any, values: Iterable[Any]) -> None:
    """``sheet.append(values)``, with text cleaned and kept as text: never a formula."""
    sheet.append([clean(v) for v in values])
    for cell in sheet[sheet.max_row]:
        if cell.data_type == "f":  # only text gets here: none of our cells is a formula
            cell.data_type = "s"
