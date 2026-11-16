"""Text into Excel safely (LDR-0035): control characters from calls dropped, text never a formula."""

from __future__ import annotations

import pytest
from openpyxl import Workbook, load_workbook

from cfr_v2 import xlsx
from cfr_v2.optimize import workbook


def test_clean_drops_what_excel_refuses_and_keeps_tab_newline_and_return():
    assert xlsx.clean("no 10\x0ba2 charges\x01") == "no 10a2 charges"
    assert xlsx.clean("a\tb\nc\rd") == "a\tb\nc\rd"
    assert xlsx.clean("".join(chr(c) for c in range(32))) == "\t\n\r"
    assert xlsx.clean(7) == 7 and xlsx.clean(None) is None


def test_append_writes_text_as_text_even_when_it_starts_with_an_equals_sign(tmp_path):
    book = Workbook()
    sheet = book.active
    xlsx.append(sheet, ['=HYPERLINK("http://x")', "plain\x0b", 3, None])
    book.save(tmp_path / "x.xlsx")
    row = load_workbook(tmp_path / "x.xlsx").active[1]
    assert [c.value for c in row] == ['=HYPERLINK("http://x")', "plain", 3]  # a trailing empty cell isn't stored
    assert row[0].data_type == "s" and row[2].data_type == "n"


def test_without_it_openpyxl_refuses_the_cell():
    """Why the helper exists: a vertical tab in a claim's reasoning stopped an export of 3k cases."""
    from openpyxl.utils.exceptions import IllegalCharacterError

    with pytest.raises(IllegalCharacterError):
        Workbook().active.append(["no 10\x0ba2 charges"])


def test_a_gold_template_with_such_text_is_written_and_reads_back_clean(tmp_path):
    path = tmp_path / "gold.xlsx"
    sheet = workbook.Sheet(
        "claims",
        ("case_no", "claim", "answer"),
        rows=[("C-1", "=SUM(A1) the agent\x1b said no", None)],
        editable=frozenset({"answer"}),
    )
    workbook.write(
        path, title="Gold\x07 set", steps=["Fill in answer."], sheets=[sheet], frozen={"C-1": "sha256:" + "0" * 64}
    )
    book = load_workbook(path)
    claim = book["claims"]["B2"]
    assert claim.value == "=SUM(A1) the agent said no" and claim.data_type == "s"
    assert book["read me"]["A1"].value == "Gold set"
