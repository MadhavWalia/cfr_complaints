"""Numbers-only inspection of the procedure PDFs (LDR-0019).

The documents can't be shared, so this measures how well extraction will work without
anyone seeing them. Reports carry the file name, counts and yes/no flags, **never document
text**: errors give the exception type only.

Measured per document:

- pages, text per page, and pages with little text (a sign of scans or image-only pages);
- tables and images found by ``pdfplumber``, and how many are real content: ones in the same
  place on most pages are page furniture (a logo, a header box), and tiny images are icons;
- whether the first pages have the expected fields (found by their labels);
- the contents pages: how many entries they list, and how many of those titles occur near
  the page they name, or anywhere after the contents. Those rates say whether sections can be
  split reliably;
- lines that look like headings by font, for documents without usable contents pages;
- lines repeated on most pages (running headers and footers, to strip before indexing).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

LOW_TEXT_CHARS = 200  # a page with less than this is probably an image, a scan or a divider
CONTENTS_SEARCH_PAGES = 8  # the contents follow page 1
CONTENTS_MIN_ENTRIES = 3  # lines on a page before it counts as a contents page
HEADING_PAGE_SLACK = 2  # printed page numbers can differ from PDF pages (cover, contents)
REPEATED_LINE_SHARE = 0.5
FRONT_PAGES = 3  # the procedure's fields are at the start, usually page 1 but sometimes page 2
SMALL_IMAGE_AREA = 0.02  # of the page: below this an image is an icon or a bullet
HEADING_SIZE_RATIO = 1.1  # against the document's most common font size
HEADING_MAX_CHARS = 120

FRONT_LABELS = {
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
    content_tables: int = 0  # at least 2 rows and 2 columns, and not in the same place on most pages
    images: int = 0
    content_images: int = 0  # not tiny, and not in the same place on most pages
    pages_with_content_images: int = 0
    front_fields: dict[str, bool] = field(default_factory=dict)
    contents_pages: int = 0
    contents_entries: int = 0
    contents_found: int = 0  # entries whose title occurs near the page they name
    contents_anywhere: int = 0  # entries whose title occurs anywhere after the contents
    font_headings: int = 0  # short lines larger than body text, or bold
    repeated_lines: int = 0  # distinct lines on at least half the pages

    @property
    def chars_per_page(self) -> int:
        return self.chars // self.pages if self.pages else 0

    @property
    def contents_match(self) -> float | None:
        return self.contents_found / self.contents_entries if self.contents_entries else None

    @property
    def contents_anywhere_match(self) -> float | None:
        return self.contents_anywhere / self.contents_entries if self.contents_entries else None

    def as_dict(self) -> dict:
        near, anywhere = self.contents_match, self.contents_anywhere_match
        return asdict(self) | {
            "chars_per_page": self.chars_per_page,
            "contents_match": None if near is None else round(near, 3),
            "contents_anywhere_match": None if anywhere is None else round(anywhere, 3),
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


def _titles_anywhere(texts: Sequence[str], contents: Sequence[int], entries: Sequence[tuple[str, int]]) -> int:
    after = max(contents, default=0) + 1
    body = " ".join(_normal(t) for t in texts[after:])
    return sum(_normal(title) in body for title, _ in entries)


def _box(item: Mapping | Sequence[float]) -> tuple[int, ...]:
    """A position to compare across pages, rounded so tiny differences don't matter."""
    edges = (item["x0"], item["top"], item["x1"], item["bottom"]) if isinstance(item, Mapping) else item
    return tuple(round(v / 5) for v in edges)


def _furniture(boxes_per_page: Sequence[Sequence[tuple[int, ...]]]) -> set[tuple[int, ...]]:
    """Positions used on most pages: a logo or header box, not content."""
    counts: Counter[tuple[int, ...]] = Counter()
    for boxes in boxes_per_page:
        counts.update(set(boxes))
    pages = len(boxes_per_page)
    return {box for box, n in counts.items() if pages > 1 and n >= pages * REPEATED_LINE_SHARE}


def _font_headings(lines_per_page: Sequence[Sequence[Mapping]]) -> int:
    sizes: Counter[float] = Counter()
    for lines in lines_per_page:
        for line in lines:
            sizes.update(round(c["size"], 1) for c in line["chars"])
    if not sizes:
        return 0
    body = sizes.most_common(1)[0][0]
    found = 0
    for lines in lines_per_page:
        for line in lines:
            chars = line["chars"]
            if not chars or len(chars) > HEADING_MAX_CHARS:
                continue
            bigger = sum(c["size"] for c in chars) / len(chars) >= body * HEADING_SIZE_RATIO
            bold = all("bold" in c["fontname"].lower() for c in chars)
            found += bigger or bold
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
        tables: list[list[tuple[tuple[int, ...], bool]]] = []  # (position, has rows and columns) per page
        images: list[list[tuple[tuple[int, ...], float]]] = []  # (position, share of page) per page
        lines: list[list[Mapping]] = []
        with pdfplumber.open(path) as pdf:
            report.pages = len(pdf.pages)
            for page in pdf.pages:
                text = page.extract_text() or ""
                texts.append(text)
                report.chars += len(text)
                report.low_text_pages += len(text.strip()) < LOW_TEXT_CHARS
                found = page.find_tables()
                report.tables += len(found)
                tables.append(
                    [(_box(t.bbox), len(t.rows) >= 2 and max(len(r.cells) for r in t.rows) >= 2) for t in found]
                )
                area = float(page.width * page.height) or 1.0
                report.images += len(page.images)
                images.append([(_box(i), (i["x1"] - i["x0"]) * (i["bottom"] - i["top"]) / area) for i in page.images])
                lines.append(page.extract_text_lines())
                page.flush_cache()
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the report; the type carries no content
        report.status = type(exc).__name__
        return report
    front = "\n".join(texts[:FRONT_PAGES])
    report.front_fields = {name: bool(p.search(front)) for name, p in FRONT_LABELS.items()}
    fixed_tables = _furniture([[box for box, _ in page] for page in tables])
    report.content_tables = sum(real and box not in fixed_tables for page in tables for box, real in page)
    fixed_images = _furniture([[box for box, _ in page] for page in images])
    per_page = [sum(box not in fixed_images and share >= SMALL_IMAGE_AREA for box, share in page) for page in images]
    report.content_images = sum(per_page)
    report.pages_with_content_images = sum(n > 0 for n in per_page)
    report.font_headings = _font_headings(lines)
    contents, entries = _find_contents(texts)
    report.contents_pages = len(contents)
    report.contents_entries = len(entries)
    report.contents_found = _entries_found(texts, contents, entries)
    report.contents_anywhere = _titles_anywhere(texts, contents, entries)
    report.repeated_lines = _repeated_lines(texts)
    return report


def inspect_pdfs(paths: Sequence[Path]) -> list[DocumentReport]:
    return [inspect_pdf(p) for p in paths]
