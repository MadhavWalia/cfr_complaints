"""Reading case numbers for ``python -m cfr_v2.intake --case-nos-from``."""

from __future__ import annotations

import pytest

from cfr_v2.intake.source import case_number, read_case_numbers


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        (1234567, "1234567"),
        (1234567.0, "1234567"),
        (" 1234567 ", "1234567"),
        ("1234567.0", "1234567"),
        ("AB-12", "AB-12"),
        (None, None),
        ("  ", None),
        (float("nan"), None),
    ],
)
def test_a_cell_becomes_the_case_number_the_query_compares(cell, expected):
    assert case_number(cell) == expected


def test_case_numbers_from_an_xlsx_column_once_each_in_order(tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "cases"
    sheet.append(["notes", " Case_No "])
    for row in (["a", 1234567], ["b", None], ["c", 7654321.0], ["d", "1234567"]):
        sheet.append(row)
    other = book.create_sheet("other")
    other.append(["case_no"])
    other.append([42])
    path = tmp_path / "cases.xlsx"
    book.save(path)
    assert read_case_numbers(path) == ["1234567", "7654321"]
    assert read_case_numbers(path, sheet="other") == ["42"]


def test_case_numbers_from_a_csv_with_another_column_name(tmp_path):
    path = tmp_path / "cases.csv"
    path.write_text("﻿case number,x\n111,a\n,b\n222,c\n111,d\n", encoding="utf-8")
    assert read_case_numbers(path, column="Case Number") == ["111", "222"]


def test_a_missing_column_or_another_file_type_is_refused(tmp_path):
    path = tmp_path / "cases.csv"
    path.write_text("id\n1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'case_no' column"):
        read_case_numbers(path)
    with pytest.raises(ValueError, match=r"\.xlsx or \.csv"):
        read_case_numbers(tmp_path / "cases.txt")


def test_the_command_takes_one_way_of_choosing_cases(tmp_path):
    from cfr_v2.intake import __main__ as intake

    path = tmp_path / "cases.csv"
    path.write_text("case_no\n1\n", encoding="utf-8")
    for argv in (
        ["--case-nos-from", str(path), "--synthetic", "1"],
        ["--case-nos-from", str(path), "--start-date", "2026-09-01"],
    ):
        with pytest.raises(SystemExit) as exc:
            intake.main(argv)
        assert exc.value.code == 2
