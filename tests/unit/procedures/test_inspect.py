"""Numbers-only PDF inspection (LDR-0019): what it measures, and that it never outputs document text."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfr_v2.procedures import inspect_pdf
from cfr_v2.procedures.__main__ import main
from cfr_v2.procedures.inspect import contents_entries

SECRET = "SECRET-BODY-TEXT"
FOOTER = "Internal Use Only"
FILLER = f"{SECRET} the account team reviews each request and records the outcome in the case file before closing it"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _page(lines: list[str], extra: str = "") -> bytes:
    text = "".join(f"BT /F1 10 Tf 50 {760 - 14 * i} Td ({_escape(line)}) Tj ET\n" for i, line in enumerate(lines))
    return (text + extra).encode("latin-1")


def _pdf(pages: list[bytes], *, with_image: bool = False) -> bytes:
    """A small but real PDF: page objects, one font, optionally one image XObject."""
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    font, image = 3 + 2 * n, 4 + 2 * n
    xobject = f"/XObject << /Im0 {image} 0 R >>" if with_image else ""
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()}
    for i, stream in enumerate(pages):
        page, content = 3 + 2 * i, 4 + 2 * i
        objects[page] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> {xobject} >> >>"
        ).encode()
        objects[content] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
    objects[font] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    if with_image:
        objects[image] = (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
            b"/BitsPerComponent 8 /Length 1 >>\nstream\n\x00\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])
    xref = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets.get(number, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


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
    assert report.tables == 1
    assert (report.images, report.pages_with_images) == (1, 1)
    assert len(report.sha256) == 64


def test_recognises_page_one_fields(report):
    assert report.page_one_fields == {
        "procedure_name": True,
        "annual_review_date": True,
        "monthly_revision_date": True,
        "custodian_approval": True,
    }


def test_contents_entries_are_checked_against_the_pages_they_name(report):
    assert (report.contents_pages, report.contents_entries) == (1, 4)
    assert report.contents_found == 3  # "4 Missing" isn't in the document
    assert report.contents_match == 0.75


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
