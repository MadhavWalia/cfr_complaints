"""The review workbook (LDR-0042 §4): written by an export, read back by a score."""

from __future__ import annotations

import pytest
from openpyxl import load_workbook

from cfr_v2.evaluation import workbook as wb
from cfr_v2.evaluation.workbook import YN, YPN, Column, Row, Sheet

SHEET = Sheet(
    "items",
    (
        Column("Case no", 12),
        Column("Item", 40, wrap=True, note="What was found"),
        Column("Correct?", 12, review=True, choices=YPN, note="For the reviewer"),
        Column("Agree?", 12, review=True, choices=YN),
        Column("Missed", 10, review=True, number=True),
        Column("Notes", 30, wrap=True, review=True),
        Column("Ref", 20, hidden=True),
    ),
    key=("Ref",),
)
CASES = Sheet(
    "cases",
    (Column("Case no", 12), Column("Missed", 10, review=True, number=True), Column("Ref", 20, hidden=True)),
    key=("Ref",),
)


def export(path, rows=None):
    rows = rows or [
        Row("items", ("C-1", "A late fee", "run-1/a1"), "run-1 sha256:aa"),
        Row("cases", ("C-1", "run-1"), "run-1 sha256:aa"),
        Row("items", ("C-1", '=HYPERLINK("x")', "run-1/a2"), "run-1 sha256:aa"),
    ]
    wb.write(path, (SHEET, CASES), rows, lambda: [("Release", "r1")])
    return path


def fill(path, sheet, cells):
    book = load_workbook(path)
    for ref, value in cells.items():
        book[sheet][ref] = value
    book.save(path)


def test_results_then_blue_review_columns_with_dropdowns_and_a_hidden_ref(tmp_path):
    book = load_workbook(export(tmp_path / "r.xlsx"))
    items = book["items"]
    assert [c.value for c in items[1]] == ["Case no", "Item", "Correct?", "Agree?", "Missed", "Notes", "Ref"]
    assert items["C1"].fill.fgColor.rgb.endswith(wb.REVIEW_FILL) and not items["A1"].fill.fgColor.rgb.endswith(
        wb.REVIEW_FILL
    )
    assert items.column_dimensions["G"].hidden
    assert items["B3"].value == '=HYPERLINK("x")' and items["B3"].data_type == "s"  # text, never a formula
    rules = {str(d.sqref): (d.type, d.formula1) for d in items.data_validations.dataValidation}
    assert rules["C2:C3"] == ("list", "=_lists!$A$1:$A$3") and rules["D2:D3"] == ("list", "=_lists!$B$1:$B$2")
    assert rules["E2:E3"][0] == "whole"
    assert book["_lists"].sheet_state == "hidden" and book["_check"].sheet_state == "hidden"
    about = [[c.value for c in r] for r in book["about"].iter_rows()]
    assert about[0] == ["Reviewer", None] and ["Release", "r1"] in about and ["items: Item", "What was found"] in about


def test_answers_are_read_back_whatever_their_case_and_spelling(tmp_path):
    path = export(tmp_path / "r.xlsx")
    fill(path, "items", {"C2": " yes ", "D2": "n", "E2": 2, "C3": "partly"})
    fill(path, "about", {"B1": "Asha"})
    review = wb.read(path, (SHEET, CASES))
    first, second = review.rows["items"]
    assert (first["Correct?"], first["Agree?"], first["Missed"]) == ("Y", "N", "2")
    assert second["Correct?"] == "Partly" and second["Agree?"] is None
    assert review.reviewer == "Asha" and review.problems == []
    assert review.stamps[("items", '["run-1/a1"]')] == "run-1 sha256:aa"


def test_an_edited_result_a_bad_answer_and_a_repeated_row_are_skipped_and_named(tmp_path):
    path = export(tmp_path / "r.xlsx")
    fill(path, "items", {"B2": "A late fee (edited)", "C3": "maybe", "A4": "C-1", "B4": "x", "G4": "run-1/a2"})
    review = wb.read(path, (SHEET, CASES))
    assert review.rows["items"] == []
    assert [(p.line, p.reason) for p in review.problems] == [
        (2, "a result cell was edited, so it no longer says what the stage said"),
        (3, "Correct?: not one of its choices"),
        (4, "the same row appears twice; the first is used"),
    ]


def test_sorting_and_deleting_rows_is_safe_because_rows_are_matched_by_key(tmp_path):
    path = export(tmp_path / "r.xlsx")
    book = load_workbook(path)
    items = book["items"]
    first = [c.value for c in items[2]]
    items.delete_rows(2)  # the reviewer deleted one row; the other moves up
    book.save(path)
    review = wb.read(path, (SHEET, CASES))
    assert [r["Ref"] for r in review.rows["items"]] == ["run-1/a2"] and first[-1] == "run-1/a1"


def test_a_deleted_ref_column_or_a_wrong_workbook_is_refused_in_words(tmp_path):
    path = export(tmp_path / "r.xlsx")
    book = load_workbook(path)
    book["items"].delete_cols(7)
    book.save(path)
    with pytest.raises(wb.WorkbookError, match="missing the column"):
        wb.read(path, (SHEET, CASES))
    with pytest.raises(wb.WorkbookError, match="no sheet 'other'"):
        wb.read(export(tmp_path / "s.xlsx"), (Sheet("other", SHEET.columns, ("Ref",)),))
    (tmp_path / "t.xlsx").write_text("not a workbook")
    with pytest.raises(wb.WorkbookError, match="can't be read"):
        wb.read(tmp_path / "t.xlsx", (SHEET,))


def test_an_older_workbook_without_the_check_sheet_or_an_optional_column_still_reads(tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "items"
    sheet.append(["Case no", "Item", "Correct?", "Agree?", "Missed", "Ref"])
    sheet.append(["C-1", "A fee", "Y", "Y", 0, "run-1/a1"])
    book.save(tmp_path / "old.xlsx")
    columns = tuple(
        c if c.header != "Notes" else Column("Notes", 30, review=True, optional=True) for c in SHEET.columns
    )
    review = wb.read(tmp_path / "old.xlsx", (Sheet("items", columns, ("Ref",)),))
    assert not review.checked and review.rows["items"][0]["Notes"] is None


def test_a_dropdown_can_offer_the_values_written_in_another_column(tmp_path):
    sheet = Sheet(
        "s",
        (Column("Vertical"), Column("Should be", review=True, choices=("NONE",), choices_seen="Vertical")),
        ("Vertical",),
    )
    wb.write(tmp_path / "v.xlsx", (sheet,), [Row("s", ("fees",)), Row("s", ("cards",))], list)
    book = load_workbook(tmp_path / "v.xlsx")
    assert [c.value for c in book["_lists"]["A"]] == ["cards", "fees", "NONE"]


def test_control_characters_from_a_call_are_dropped_not_fatal(tmp_path):
    path = export(tmp_path / "r.xlsx", [Row("items", ("C-1\x00", "no 10\x0ba2 charges\x01\nand\tso", "run-1/a1"))])
    cells = [c.value for c in load_workbook(path)["items"][2]]
    assert cells[:2] == ["C-1", "no 10a2 charges\nand\tso"]  # newline and tab are kept
    assert wb.read(path, (SHEET, CASES)).problems == []  # and the fingerprint still matches
