"""Procedure extraction (LDR-0019): front-page fields, sections from contents titles, tables, page furniture."""

from __future__ import annotations

from datetime import date

import pytest

from cfr_v2.procedures import extract_pdf, inspect_pdf
from cfr_v2.procedures.extract import (
    MAX_SECTION_CHARS,
    MIN_SECTION_CHARS,
    OPENING_TITLE,
    date_from_file_name,
    dates_in,
    front_dates,
    front_name,
    procedure_key,
)
from tests.unit.procedures.procedure_pdfs import _page, _pdf, grid, text_at

FOOTER = "Internal Use Only"
BODY = "The account team reviews each request and records the outcome in the case file before closing it"
FRONT = [
    "Procedure Name: Global Example Procedure",
    "Annual Review Effective Date: 1 March 2026",
    "Monthly Revision Effective Date: October 3, 2026",
    "Custodian Approval Required: Yes",
]


def _footed(lines: list[str], number: int, extra: str = "") -> bytes:
    return _page(lines, extra + text_at(50, 30, FOOTER) + text_at(450, 30, f"Page {number} of 6"))


def _document() -> bytes:
    """The contents' page numbers are wrong on purpose, and "9 Missing" isn't in the document."""
    table = grid([["Reason", "Team"], ["Late fee", "Collections"], ["Lost card", "Servicing"]])
    return _pdf(
        [
            _footed(FRONT, 1),
            _footed(
                ["Table of Contents", "1 Purpose ...... 40", "2 Scope ...... 41", "3 Rules ...... 42", "9 Missing ... 43"],
                2,
            ),
            _footed(["1 Purpose", BODY, BODY], 3),
            _footed([f"Continuing, {BODY}", "2 Scope", BODY], 4),
            _footed(["3 Rules", BODY], 5, table),
            _footed([f"Finally, {BODY}"], 6),
        ]
    )


@pytest.fixture(scope="module")
def document():
    return extract_pdf(_document())


def test_reads_the_front_page(document):
    assert document.name == "Global Example Procedure"
    assert document.annual_review_date == date(2026, 3, 1)
    assert document.monthly_revision_date == date(2026, 10, 3)


def test_sections_come_from_contents_titles_and_cite_the_page_they_were_found_on(document):
    assert document.method == "contents"
    assert [(s.title, s.page_start, s.page_end) for s in document.sections] == [
        ("1 Purpose", 3, 4),
        ("2 Scope", 4, 4),
        ("3 Rules", 5, 6),
    ]
    assert (document.contents_entries, document.contents_located, document.skipped_pages) == (4, 3, 2)


def test_tables_become_markdown_in_place_and_their_text_is_not_repeated(document):
    rules = document.sections[2].text
    assert document.tables == 1
    assert "| Reason | Team |\n| --- | --- |\n| Late fee | Collections |\n| Lost card | Servicing |" in rules
    assert rules.count("Late fee") == 1
    assert rules.index("3 Rules") < rules.index("| Reason")


def test_repeated_header_and_footer_lines_are_dropped_even_with_changing_numbers(document):
    text = "\n".join(s.text for s in document.sections)
    assert FOOTER not in text and "Page 3 of 6" not in text


def test_a_repeated_table_box_is_furniture():
    header = grid([["Owner", "Global Ops"], ["Status", "Final"]], top=700)
    pages = [_page(["Heading", BODY], header) for _ in range(4)]
    document = extract_pdf(_pdf(pages))
    assert document.tables == 0
    assert "Global Ops" not in "\n".join(s.text for s in document.sections)


LOGO = "q 200 0 0 60 50 725 cm /Im0 Do Q\n"  # 2.5% of the page: too big to pass as an icon
FOOTER_PHOTO = "q 200 0 0 60 350 10 cm /Im0 Do Q\n"
HEADER = "Global Example Procedure Header"


def _with_furniture(number: int, body: list[str], extra: str = "") -> bytes:
    """A header line and logo, a footer line and photo, and the body well clear of both."""
    lines = "".join(text_at(50, 640 - 14 * i, line) for i, line in enumerate(body))
    margins = text_at(50, 775, HEADER) + text_at(50, 30, f"{FOOTER} Page {number} of 4")
    return (LOGO + FOOTER_PHOTO + margins + lines + extra).encode("latin-1")


def _furnished_document(extra: str = "") -> bytes:
    bodies = (["First page text"], ["Second page text"], ["Third page text"], ["Fourth page text"])
    pages = [_with_furniture(i, body, extra) for i, body in enumerate(bodies, start=1)]
    return _pdf(pages, with_image=True)


def test_headers_and_footers_with_pictures_leave_only_the_body():
    document = extract_pdf(_furnished_document())
    text = "\n".join(s.text for s in document.sections)
    assert text.splitlines() == ["First page text", "Second page text", "Third page text", "Fourth page text"]
    assert document.tables == 0


def test_pictures_in_the_margins_are_furniture_to_inspect_too(tmp_path):
    path = tmp_path / "furnished.pdf"
    path.write_bytes(_furnished_document())
    report = inspect_pdf(path)
    assert (report.images, report.content_images, report.pages_with_content_images) == (8, 0, 0)


def test_a_picture_in_the_body_adds_nothing_and_loses_nothing():
    document = extract_pdf(_furnished_document("q 200 0 0 200 100 200 cm /Im0 Do Q\n"))
    assert "\n".join(s.text for s in document.sections).count("page text") == 4


def test_a_line_in_the_margin_of_one_page_only_is_kept():
    pages = [_with_furniture(i, ["Body text"]) for i in range(1, 4)]
    pages.append(_with_furniture(4, ["Body text"], text_at(50, 755, "Only on this page")))
    document = extract_pdf(_pdf(pages, with_image=True))
    text = "\n".join(s.text for s in document.sections)
    assert "Only on this page" in text and HEADER not in text


def test_without_contents_sections_come_from_larger_or_bold_lines():
    long = [BODY] * 3
    lines = text_at(50, 760, "Intake Rules", size=14)
    lines += "".join(text_at(50, 740 - 14 * i, t) for i, t in enumerate(long))
    lines += text_at(50, 680, "Escalation", font="F2")
    lines += "".join(text_at(50, 660 - 14 * i, t) for i, t in enumerate(long))
    document = extract_pdf(_pdf([lines.encode("latin-1")]))
    assert document.method == "font" and document.skipped_pages == 0
    assert [s.title for s in document.sections] == ["Intake Rules", "Escalation"]


def test_short_font_sections_join_the_next_one():
    lines = text_at(50, 760, "Tiny", size=14) + text_at(50, 740, "one line")
    lines += text_at(50, 720, "Full", size=14) + "".join(text_at(50, 700 - 14 * i, BODY) for i in range(3))
    document = extract_pdf(_pdf([lines.encode("latin-1")]))
    assert len(BODY) * 3 > MIN_SECTION_CHARS
    assert [s.title for s in document.sections] == ["Tiny"]
    assert "Full" in document.sections[0].text


def test_a_document_with_no_headings_is_one_section():
    document = extract_pdf(_pdf([_page([BODY, BODY])]))
    assert [s.title for s in document.sections] == [OPENING_TITLE]


def test_long_sections_are_split_into_parts():
    pages = [_page(["Table of Contents", "1 Rules ...... 3", "2 End ...... 9", "3 Other ...... 9"])]
    pages[0:0] = [_page(FRONT)]
    pages += [_page((["1 Rules"] if i == 0 else []) + [BODY] * 40) for i in range(4)]
    pages += [_page(["2 End", BODY]), _page(["3 Other", BODY])]
    document = extract_pdf(_pdf(pages))
    rules = [s for s in document.sections if s.title == "1 Rules"]
    assert [s.part for s in rules] == list(range(1, len(rules) + 1)) and len(rules) > 1
    assert all(len(s.text) <= MAX_SECTION_CHARS for s in rules) and document.oversized_sections == 0
    assert rules[0].page_start == 3 and rules[-1].page_end == 6


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("September 18, 2026", date(2026, 9, 18)),
        ("18 Sep 2026", date(2026, 9, 18)),
        ("Sept. 2026", date(2026, 9, 1)),
        ("09/18/2026", date(2026, 9, 18)),
        ("18/09/2026", date(2026, 9, 18)),
        ("2026-09-18", date(2026, 9, 18)),
    ],
)
def test_date_forms(text, expected):
    assert dates_in(f"Effective {text} until replaced") == [expected]


def test_words_that_start_like_months_are_not_dates():
    assert dates_in("Marketing 2026 and Decision 2026, Section 12 2026") == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Global Corporate RLA Procedure Sep'26.pdf", date(2026, 9, 1)),
        ("Global Corporate Case Setup Procedure_Aug_26.pdf", date(2026, 8, 1)),
        ("Global New Accounts Operating Procedure Aug26.pdf", date(2026, 8, 1)),
        ("US CLP APR Procedure 202609.pdf", date(2026, 9, 1)),
        ("Credit Monitoring Procedure - Sep 18 2026 (1).pdf", date(2026, 9, 18)),
        ("LED Procedure.pdf", None),
    ],
)
def test_dates_in_file_names(name, expected):
    assert date_from_file_name(name) == expected


def test_versions_and_copies_share_a_key():
    names = ["6.Global Corporate RLA Procedure Aug'26.pdf", "Global Corporate RLA Procedure Sep'26.pdf"]
    assert {procedure_key(n) for n in names} == {"global corporate rla procedure"}
    assert procedure_key("Pay Over Time September 2026 (1).pdf") == procedure_key("Pay_Over_Time_Sep'26.pdf")
    assert procedure_key("Blueprint Line Procedure as of 2026-09-01.pdf") == "blueprint line procedure"


@pytest.mark.parametrize(
    "front",
    [
        "Annual Review Effective Date: March 2026\nMonthly Revision Effective Date: Oct 3, 2026",
        "Annual Review Effective Date Monthly Revision Effective Date\nMarch 2026 Oct 3, 2026",
        "Annual Review Date Monthly Revision Date Custodian Approval\nMarch 2026 Oct 3, 2026 Yes",
        "Annual Review Date\nMarch 2026\nMonthly Revision Date\nOct 3, 2026",
    ],
)
def test_front_dates_beside_or_below_their_labels(front):
    assert front_dates(front) == (date(2026, 3, 1), date(2026, 10, 3))


def test_a_missing_date_is_left_empty():
    assert front_dates("Annual Review Date: March 2026\nCustodian: Yes") == (date(2026, 3, 1), None)
    assert front_dates("Monthly Revision Date: to be confirmed\nCustodian: Yes") == (None, None)


def test_front_name_forms():
    assert front_name("Procedure Name: Global Example\nAnnual Review: x") == "Global Example"
    assert front_name("Procedure Name\nGlobal Example\nAnnual Review: x") == "Global Example"
    assert front_name("Procedure Name: Global Example Custodian: Team") == "Global Example"
    assert front_name("Global Example Procedure\nCustodian: x") == "Global Example Procedure"
    assert front_name("Custodian procedure approval\nNothing here") is None
