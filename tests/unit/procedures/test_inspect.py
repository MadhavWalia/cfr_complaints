"""Numbers-only PDF inspection (LDR-0019): what it measures, and that it never outputs document text."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfr_v2.procedures import inspect_pdf
from cfr_v2.procedures.__main__ import main
from cfr_v2.procedures.inspect import contents_entries
from tests.unit.procedures.procedure_pdfs import _page, _pdf

SECRET = "SECRET-BODY-TEXT"
FOOTER = "Internal Use Only"
FILLER = f"{SECRET} the account team reviews each request and records the outcome in the case file before closing it"


GRID = "".join(f"{x} {y} 120 30 re S\n" for x in (50, 170, 290) for y in (300, 330, 360))
IMAGE = "q 200 0 0 200 100 100 cm /Im0 Do Q\n"


def _procedure(path: Path) -> Path:
    body = [FILLER] * 6 + [FOOTER]
    pages = [
        _page(
            [
                "Procedure Name: Global Example",
                "Annual Review Effective Date: 1 March 2026",
                "Monthly Revision Effective Date: 1 October 2026",
                "Custodian Approval Required: Yes",
                FOOTER,
            ]
        ),
        _page(
            [
                "Table of Contents",
                "1 Purpose ........ 3",
                "2 Scope ........ 4",
                "3 Rules ........ 5",
                "4 Missing ... 5",
                FOOTER,
            ]
        ),
        _page(["1 Purpose", *body]),
        _page(["2 Scope", *body], GRID),
        _page(["3 Rules", *body], IMAGE),
        _page([FOOTER]),  # nearly empty page
    ]
    path.write_bytes(_pdf(pages, with_image=True))
    return path


@pytest.fixture
def report(tmp_path):
    return inspect_pdf(_procedure(tmp_path / "Global Example.pdf"))


def test_measures_pages_text_tables_and_images(report):
    assert (report.status, report.pages) == ("ok", 6)
    assert report.chars_per_page > 100
    assert report.low_text_pages == 3  # page 1, contents and the near-empty page
    assert (report.tables, report.content_tables) == (1, 1)
    assert (report.images, report.content_images, report.pages_with_content_images) == (1, 1, 1)
    assert len(report.sha256) == 64


def test_recognises_the_front_fields(report):
    assert report.front_fields == {
        "procedure_name": True,
        "annual_review_date": True,
        "monthly_revision_date": True,
        "custodian_approval": True,
    }


def test_contents_entries_are_checked_against_the_pages_they_name(report):
    assert (report.contents_pages, report.contents_entries) == (1, 4)
    assert report.contents_found == 3  # "4 Missing" isn't in the document
    assert report.contents_match == 0.75
    assert report.contents_anywhere == 3 and report.contents_anywhere_match == 0.75


def test_front_fields_can_be_on_page_two(tmp_path):
    path = tmp_path / "late.pdf"
    fields = ["Procedure Name: X", "Annual Review: 1", "Monthly Revision: 2", "Custodian Approval: Yes"]
    path.write_bytes(_pdf([_page(["Cover"]), _page(fields), _page(["Body"])]))
    assert all(inspect_pdf(path).front_fields.values())


def test_logo_and_header_box_in_the_same_place_are_not_content(tmp_path):
    path = tmp_path / "furniture.pdf"
    logo = "q 200 0 0 100 300 650 cm /Im0 Do Q\n"
    icon = "q 10 0 0 10 50 50 cm /Im0 Do Q\n"
    pages = [_page([FILLER], logo + GRID) for _ in range(4)] + [_page([FILLER], icon)]
    path.write_bytes(_pdf(pages, with_image=True))
    report = inspect_pdf(path)
    assert (report.images, report.content_images) == (5, 0)  # four logos and one icon
    assert (report.tables, report.content_tables) == (4, 0)


def test_contents_titles_found_elsewhere_are_counted(tmp_path):
    path = tmp_path / "offset.pdf"
    contents = ["Contents", "1 Purpose ... 9", "2 Scope ... 9", "3 Rules ... 9"]  # stale page numbers
    pages = [_page(["Cover"]), _page(contents), _page(["1 Purpose", "2 Scope", "3 Rules"]), _page(["End"])]
    path.write_bytes(_pdf(pages))
    report = inspect_pdf(path)
    assert (report.contents_found, report.contents_anywhere) == (0, 3)


def test_larger_lines_count_as_font_headings(tmp_path):
    path = tmp_path / "fonts.pdf"
    heading = "BT /F1 16 Tf 50 500 Td (A Larger Heading) Tj ET\n"
    path.write_bytes(_pdf([_page([FILLER] * 5, heading)]))
    assert inspect_pdf(path).font_headings == 1


def test_identical_files_are_reported_once(tmp_path, capsys):
    folder = tmp_path / "pdfs"
    folder.mkdir()
    first = _procedure(folder / "Example.pdf")
    (folder / "Example (1).pdf").write_bytes(first.read_bytes())
    assert main(["inspect", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "2 of 2 files read; 1 distinct by content" in out
    assert "Example (1).pdf = Example.pdf" in out


def test_running_footer_is_counted_as_repeated(report):
    assert report.repeated_lines >= 1


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("3.2 Line Reduction ........... 14", [("3.2 Line Reduction", 14)]),
        ("Appendix A 52", [("Appendix A", 52)]),
        ("Effective for all markets", []),
    ],
)
def test_contents_line_shapes(line, expected):
    assert contents_entries(line) == expected


def test_unreadable_file_is_reported_not_fatal(tmp_path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"not a pdf at all")
    report = inspect_pdf(bad)
    assert report.status != "ok" and report.pages == 0


def test_output_holds_numbers_and_file_names_only(tmp_path, capsys):
    folder = tmp_path / "pdfs"
    folder.mkdir()
    _procedure(folder / "Global Example.pdf")
    (folder / "broken.pdf").write_bytes(b"not a pdf at all")
    out = tmp_path / "inspect.json"

    assert main(["inspect", str(folder), "--out", str(out)]) == 0

    captured = capsys.readouterr()
    written = captured.out + captured.err + out.read_text()
    for text in (SECRET, "reviews each request", FOOTER, "Purpose", "1 March 2026", "Procedure Name:"):
        assert text not in written
    assert "Global Example.pdf" in written and "could not read" in written
    assert json.loads(out.read_text())[0]["contents_match"] == 0.75


def test_empty_folder_is_an_error(tmp_path, capsys):
    assert main(["inspect", str(tmp_path)]) == 1
    assert "no PDF files" in capsys.readouterr().err
