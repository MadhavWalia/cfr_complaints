"""Numbers-only inspection of the procedure PDFs (LDR-0019).

The documents can't be shared, so this measures how well extraction will work without
anyone seeing them. Reports carry the file name, counts and yes/no flags, **never document
text**: errors give the exception type only.

Measured per document:

- pages, text per page, and pages with little text (a sign of scans or image-only pages);
- tables and images found by ``pdfplumber``;
- whether page 1 has the expected fields (found by their labels);
- the contents pages: how many entries they list, and how many of those titles occur near
  the page they name. That rate says whether sections can be split reliably;
- lines repeated on most pages (running headers and footers, to strip before indexing).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

LOW_TEXT_CHARS = 200  # a page with less than this is probably an image, a scan or a divider
CONTENTS_SEARCH_PAGES = 8  # the contents follow page 1
CONTENTS_MIN_ENTRIES = 3  # lines on a page before it counts as a contents page
HEADING_PAGE_SLACK = 2  # printed page numbers can differ from PDF pages (cover, contents)
REPEATED_LINE_SHARE = 0.5

PAGE_ONE_LABELS = {
    "procedure_name": re.compile(r"procedure\s+name|^\s*procedure\b", re.IGNORECASE | re.MULTILINE),
    "annual_review_date": re.compile(r"annual\s+review", re.IGNORECASE),
    "monthly_revision_date": re.compile(r"monthly\s+revision", re.IGNORECASE),
    "custodian_approval": re.compile(r"custodian", re.IGNORECASE),
}
_CONTENTS_TITLE = re.compile(r"table\s+of\s+contents|^\s*contents\s*$", re.IGNORECASE | re.MULTILINE)
# "1.2 Title ........ 14", "Title 14": a title, then a page number ending the line.
_CONTENTS_LINE = re.compile(r"^(?P<title>\S.{2,}?)(?:\s*[.…·_]{2,}\s*|\s+)(?P<page>\d{1,3})\s*$")
_DOT_LEADER = re.compile(r"[.…·_]{3,}")


def _normal(text: str) -> str:
    return " ".join(text.lower().split())


@dataclass
class DocumentReport:
    document: str  # file name
    sha256: str = ""  # of the file: tells versions apart without showing content
    status: str = "ok"  # ok, or the exception type that stopped the read
    pages: int = 0
    chars: int = 0
    low_text_pages: int = 0
    tables: int = 0
    images: int = 0
    pages_with_images: int = 0
    page_one_fields: dict[str, bool] = field(default_factory=dict)
    contents_pages: int = 0
    contents_entries: int = 0
    contents_found: int = 0  # entries whose title occurs near the page they name
    repeated_lines: int = 0  # distinct lines on at least half the pages

    @property
    def chars_per_page(self) -> int:
        return self.chars // self.pages if self.pages else 0

    @property
    def contents_match(self) -> float | None:
        return self.contents_found / self.contents_entries if self.contents_entries else None

    def as_dict(self) -> dict:
        match = self.contents_match
        return asdict(self) | {
            "chars_per_page": self.chars_per_page,
            "contents_match": None if match is None else round(match, 3),
        }


def contents_entries(page_text: str) -> list[tuple[str, int]]:
    """``(title, printed page)`` for each line that looks like a contents entry."""
    entries = []
    for line in page_text.splitlines():
        m = _CONTENTS_LINE.match(line.strip())
        if m:
            entries.append((_DOT_LEADER.sub(" ", m["title"]).strip(), int(m["page"])))
    return entries


def _find_contents(texts: Sequence[str]) -> tuple[list[int], list[tuple[str, int]]]:
    """Contents pages after page 1 (0-based), and their entries. They end at the first page without entries."""
    pages: list[int] = []
    entries: list[tuple[str, int]] = []
    for index in range(1, min(len(texts), CONTENTS_SEARCH_PAGES + 1)):
        found = contents_entries(texts[index])
        titled = bool(_CONTENTS_TITLE.search(texts[index]))
        dotted = bool(_DOT_LEADER.search(texts[index]))
        if len(found) >= CONTENTS_MIN_ENTRIES and (titled or dotted or pages):
            pages.append(index)
            entries += found
        elif pages:
            break
    return pages, entries


def _entries_found(texts: Sequence[str], contents: Sequence[int], entries: Sequence[tuple[str, int]]) -> int:
    normal = [_normal(t) for t in texts]
    skip = set(contents)
    found = 0
    for title, printed in entries:
        wanted = _normal(title)
        low = max(printed - 1 - HEADING_PAGE_SLACK, 0)
        high = min(printed - 1 + HEADING_PAGE_SLACK, len(texts) - 1)
        found += any(wanted in normal[i] for i in range(low, high + 1) if i not in skip)
    return found


def _repeated_lines(texts: Sequence[str]) -> int:
    counts: Counter[str] = Counter()
    for text in texts:
        counts.update({_normal(line) for line in text.splitlines() if line.strip()})
    return sum(1 for n in counts.values() if len(texts) > 1 and n >= len(texts) * REPEATED_LINE_SHARE)


def inspect_pdf(path: Path) -> DocumentReport:
    import pdfplumber

    logging.getLogger("pdfminer").setLevel(logging.ERROR)  # its warnings can quote content
    report = DocumentReport(document=path.name)
    try:
        report.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        texts: list[str] = []
        with pdfplumber.open(path) as pdf:
            report.pages = len(pdf.pages)
            for page in pdf.pages:
                text = page.extract_text() or ""
                texts.append(text)
                report.chars += len(text)
                report.low_text_pages += len(text.strip()) < LOW_TEXT_CHARS
                report.tables += len(page.find_tables())
                report.images += len(page.images)
                report.pages_with_images += bool(page.images)
                page.flush_cache()
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the report; the type carries no content
        report.status = type(exc).__name__
        return report
    first = texts[0] if texts else ""
    report.page_one_fields = {name: bool(p.search(first)) for name, p in PAGE_ONE_LABELS.items()}
    contents, entries = _find_contents(texts)
    report.contents_pages = len(contents)
    report.contents_entries = len(entries)
    report.contents_found = _entries_found(texts, contents, entries)
    report.repeated_lines = _repeated_lines(texts)
    return report


def inspect_pdfs(paths: Sequence[Path]) -> list[DocumentReport]:
    return [inspect_pdf(p) for p in paths]
