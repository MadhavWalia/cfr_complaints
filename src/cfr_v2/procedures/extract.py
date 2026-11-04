"""Extract a procedure PDF into front-page fields and sections (LDR-0019).

Text and tables only; images are ignored for now. Measured on the real documents:

- **Sections come from the contents titles,** found as lines in the body, not from the page
  numbers the contents give: those can be stale. Each section cites the page it was found on.
- **Font size over-detects headings,** so it is used only for documents without contents, and to
  choose where a long section splits.
- **Repeated page furniture is dropped:** tables in the same place on most pages, and lines
  repeated on most pages in the top or bottom margin.

The result is unredacted. ``corpus.py`` redacts it before anything is stored or indexed.
"""

from __future__ import annotations

import io
import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .inspect import (
    FRONT_LABELS,
    FRONT_PAGES,
    HEADING_MAX_CHARS,
    HEADING_SIZE_RATIO,
    REPEATED_LINE_SHARE,
    _box,
    _find_contents,
    _furniture,
    _normal,
)

EXTRACTOR_VERSION = "pdf-sections-1"
MAX_SECTION_CHARS = 6000  # about 1,500 tokens: one passage for retrieval
MIN_SECTION_CHARS = 200  # with font headings only: shorter ones join the next section
MARGIN_SHARE = 0.12  # of the page height, at top and bottom
DATE_WINDOW_CHARS = 160  # how far after a label its date can be
OPENING_TITLE = "Opening text"  # text before the first heading

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH_WORD = "(?P<month>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_MONTH = _MONTH_WORD + r"\b\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>(?:19|20)\d{2})"
# Most specific first. Numeric dates are read as month/day, unless the first number can't be a month.
_DATE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        rf"\b{_YEAR}-(?P<month>\d{{2}})-{_DAY}\b",
        rf"\b{_MONTH}[\s-]+{_DAY},?[\s-]+{_YEAR}\b",
        rf"\b{_DAY}[\s-]+{_MONTH},?[\s-]+{_YEAR}\b",
        rf"\b(?P<month>\d{{1,2}})[/-]{_DAY}[/-]{_YEAR}\b",
        rf"\b{_MONTH},?[\s-]+{_YEAR}\b",
    )
)
# Only in file names: Sep'26, Aug_26, Aug26, 202609.
_SHORT_DATE_PATTERNS = (
    re.compile(rf"\b{_MONTH_WORD}[\s_'’-]*(?P<year>\d{{2}})\b", re.IGNORECASE),
    re.compile(r"\b(?P<year>20\d{2})(?P<month>0[1-9]|1[0-2])\b"),
)
_NAME_LABEL = re.compile(r"procedure\s+(?:name|title)\b\s*[:|–-]?[ \t]*(?P<value>.*)", re.IGNORECASE)
_DATE_LABELS = {
    "annual": re.compile(r"annual\s+review", re.IGNORECASE),
    "monthly": re.compile(r"monthly\s+revision", re.IGNORECASE),
}


def _to_date(match: re.Match) -> date | None:
    month, day, year = match["month"], match.groupdict().get("day"), int(match["year"])
    number = _MONTHS[month.lower()] if month.isalpha() else int(month)
    day_number = int(day) if day else 1
    if number > 12 and day_number <= 12:
        number, day_number = day_number, number
    try:
        return date(year + 2000 if year < 100 else year, number, day_number)
    except ValueError:
        return None


def _date_matches(text: str, patterns: Sequence[re.Pattern]) -> list[tuple[int, int, date]]:
    """Non-overlapping dates in the order they appear. Earlier patterns win an overlap."""
    found: list[tuple[int, int, date]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            value = _to_date(match)
            if value and not any(match.start() < end and start < match.end() for start, end, _ in found):
                found.append((match.start(), match.end(), value))
    return sorted(found)


def dates_in(text: str) -> list[date]:
    return [value for _, _, value in _date_matches(text, _DATE_PATTERNS)]


def date_from_file_name(file_name: str) -> date | None:
    stem = file_name.rsplit(".", 1)[0].replace("_", " ")
    found = _date_matches(stem, (*_DATE_PATTERNS, *_SHORT_DATE_PATTERNS))
    return found[0][2] if found else None


def procedure_key(name: str) -> str:
    """The same for every version of a procedure: no dates, copy numbers, numbering or punctuation."""
    text = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    text = re.sub(r"\(\d+\)", " ", text).replace("_", " ")
    spans = _date_matches(text, (*_DATE_PATTERNS, *_SHORT_DATE_PATTERNS))
    for start, end, _ in reversed(spans):
        text = text[:start] + " " + text[end:]
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return re.sub(r"\s+as of$", "", re.sub(r"^\d+\s+", "", text))


def front_dates(front: str) -> tuple[date | None, date | None]:
    """``(annual review, monthly revision)``. Handles a value beside its label, and a table with
    the labels in one row and the values in the next."""
    labels = sorted((m.start(), m.end(), name) for name, p in _DATE_LABELS.items() if (m := p.search(front)))
    stops = sorted(m.start() for p in FRONT_LABELS.values() for m in p.finditer(front))
    windows: list[tuple[str, list[date]]] = []
    for _, end, name in labels:
        stop = min((s for s in stops if s >= end), default=len(front))
        windows.append((name, dates_in(front[end : min(stop, end + DATE_WINDOW_CHARS)])))
    values: dict[str, date] = {}
    if windows and not any(dates for _, dates in windows):
        pool = dates_in(front[labels[-1][1] : labels[-1][1] + 2 * DATE_WINDOW_CHARS])
        values = {name: value for (name, _), value in zip(windows, pool, strict=False)}
    for i, (name, dates) in enumerate(windows):
        if dates:
            values[name] = dates.pop(0)
        elif i + 1 < len(windows) and len(windows[i + 1][1]) >= 2:
            values[name] = windows[i + 1][1].pop(0)
    return values.get("annual"), values.get("monthly")


def front_name(front: str) -> str | None:
    lines = [line.strip() for line in front.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        match = _NAME_LABEL.search(line)
        if match:
            value = match["value"].strip() or (lines[i + 1] if i + 1 < len(lines) else "")
            for label in FRONT_LABELS.values():  # the next label can be on the same line
                value = value[: m.start()] if (m := label.search(value)) and m.start() else value
            return value.strip() or None
    return next(
        (x for x in lines[:5] if "procedure" in x.lower() and len(x) <= HEADING_MAX_CHARS and not _is_field(x)), None
    )


def _is_field(line: str) -> bool:
    return any(p.search(line) for name, p in FRONT_LABELS.items() if name != "procedure_name")


@dataclass(frozen=True)
class Line:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool


@dataclass(frozen=True)
class Table:
    bbox: tuple[float, float, float, float]
    rows: tuple[tuple[str | None, ...], ...]

    @property
    def real(self) -> bool:
        return len(self.rows) >= 2 and max(len(r) for r in self.rows) >= 2 and any(any(r) for r in self.rows)

    def holds(self, line: Line) -> bool:
        x, y = (line.x0 + line.x1) / 2, (line.top + line.bottom) / 2
        return self.bbox[0] <= x <= self.bbox[2] and self.bbox[1] <= y <= self.bbox[3]


@dataclass(frozen=True)
class Page:
    number: int
    height: float
    lines: tuple[Line, ...]
    tables: tuple[Table, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True)
class Block:
    page: int
    text: str
    table: bool = False
    heading: bool = False  # by font


@dataclass(frozen=True)
class ExtractedSection:
    title: str
    part: int
    page_start: int
    page_end: int
    text: str


@dataclass
class ExtractedDocument:
    pages: int
    name: str | None
    annual_review_date: date | None
    monthly_revision_date: date | None
    method: Literal["contents", "font"]
    contents_entries: int = 0
    contents_located: int = 0
    tables: int = 0
    skipped_pages: int = 0
    sections: list[ExtractedSection] = field(default_factory=list)

    @property
    def oversized_sections(self) -> int:
        return sum(len(s.text) > MAX_SECTION_CHARS for s in self.sections)


def _line(raw: Mapping) -> Line:
    chars = raw["chars"]
    return Line(
        text=raw["text"].strip(),
        x0=raw["x0"],
        x1=raw["x1"],
        top=raw["top"],
        bottom=raw["bottom"],
        size=sum(c["size"] for c in chars) / len(chars) if chars else 0.0,
        bold=bool(chars) and all("bold" in c["fontname"].lower() for c in chars),
    )


def read_pages(data: bytes) -> list[Page]:
    import pdfplumber

    logging.getLogger("pdfminer").setLevel(logging.ERROR)  # its warnings can quote content
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            lines = tuple(_line(raw) for raw in page.extract_text_lines() if raw["text"].strip())
            tables = tuple(Table(t.bbox, tuple(tuple(r) for r in t.extract())) for t in page.find_tables())
            pages.append(Page(number, float(page.height), lines, tables))
            page.flush_cache()
    return pages


def _in_margin(line: Line, height: float) -> bool:
    return line.top < height * MARGIN_SHARE or line.bottom > height * (1 - MARGIN_SHARE)


def _shape(text: str) -> str:
    return re.sub(r"\d+", "#", _normal(text))  # "Page 3 of 20" repeats too


def _repeated_margin_lines(pages: Sequence[Page]) -> set[str]:
    counts: Counter[str] = Counter()
    for page in pages:
        counts.update({_shape(line.text) for line in page.lines if _in_margin(line, page.height)})
    return {text for text, n in counts.items() if n >= max(2, len(pages) * REPEATED_LINE_SHARE)}


def _without_margin_lines(pages: Sequence[Page]) -> list[str]:
    repeated = _repeated_margin_lines(pages)
    return [
        "\n".join(
            line.text
            for line in page.lines
            if not (_in_margin(line, page.height) and _shape(line.text) in repeated)
        )
        for page in pages
    ]


def _markdown(rows: Sequence[Sequence[str | None]]) -> str:
    cells = [[" ".join((c or "").split()).replace("|", "\\|") for c in row] for row in rows]
    cells = [row for row in cells if any(row)]
    if not cells:
        return ""
    width = max(len(row) for row in cells)
    cells = [row + [""] * (width - len(row)) for row in cells]
    lines = ["| " + " | ".join(row) + " |" for row in cells]
    return "\n".join([lines[0], "|" + " --- |" * width, *lines[1:]])


def _body_size(pages: Sequence[Page]) -> float:
    sizes: Counter[float] = Counter()
    for page in pages:
        for line in page.lines:
            sizes[round(line.size, 1)] += len(line.text)
    return sizes.most_common(1)[0][0] if sizes else 0.0


def _is_heading(line: Line, body: float) -> bool:
    if len(line.text) > HEADING_MAX_CHARS or line.text.endswith((".", ",", ";", ":")):
        return False
    return line.bold or (body > 0 and line.size >= body * HEADING_SIZE_RATIO)


def _blocks(pages: Sequence[Page]) -> list[Block]:
    """Lines and tables in reading order, without page furniture."""
    fixed = _furniture([[_box(t.bbox) for t in page.tables] for page in pages])
    repeated = _repeated_margin_lines(pages)
    body = _body_size(pages)
    blocks: list[Block] = []
    for page in pages:
        dropped = [t for t in page.tables if t.real or _box(t.bbox) in fixed]
        placed: list[tuple[float, Block]] = []
        for line in page.lines:
            if any(t.holds(line) for t in dropped):
                continue
            if _in_margin(line, page.height) and _shape(line.text) in repeated:
                continue
            placed.append((line.top, Block(page.number, line.text, heading=_is_heading(line, body))))
        for table in page.tables:
            if table.real and _box(table.bbox) not in fixed and (text := _markdown(table.rows)):
                placed.append((table.bbox[1], Block(page.number, text, table=True)))
        blocks += [block for _, block in sorted(placed, key=lambda item: item[0])]
    return blocks


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _is_title(blocks: Sequence[Block], index: int, wanted: str) -> bool:
    block = blocks[index]
    if block.table:
        return False
    line = _words(block.text)
    if line == wanted or (block.heading and line.startswith(wanted)):
        return True
    if line and len(line) < len(wanted) and wanted.startswith(line) and index + 1 < len(blocks):  # wrapped title
        return not blocks[index + 1].table and _words(f"{block.text} {blocks[index + 1].text}") == wanted
    return False


def _locate(blocks: Sequence[Block], titles: Sequence[str]) -> list[tuple[int, str]]:
    """Where each title starts, keeping the contents' order. A title not found is left out, and
    its text stays in the section before."""
    found: list[tuple[int, str]] = []
    position = 0
    for title in titles:
        wanted = _words(title)
        hit = next((i for i in range(position, len(blocks)) if wanted and _is_title(blocks, i, wanted)), None)
        if hit is not None:
            found.append((hit, title))
            position = hit + 1
    return found


def _font_starts(blocks: Sequence[Block]) -> list[tuple[int, str]]:
    """The first of each run of heading lines."""
    return [
        (i, block.text)
        for i, block in enumerate(blocks)
        if block.heading and not (i and blocks[i - 1].heading)
    ]


def _join(blocks: Sequence[Block]) -> str:
    parts: list[str] = []
    for i, block in enumerate(blocks):
        if i:
            parts.append("\n\n" if block.table or blocks[i - 1].table else "\n")
        parts.append(block.text)
    return "".join(parts)


def _group(blocks: Sequence[Block], starts: Sequence[tuple[int, str]]) -> list[tuple[str, list[Block]]]:
    edges = list(starts)
    if not edges or edges[0][0] > 0:
        edges.insert(0, (0, OPENING_TITLE))
    bounds = [i for i, _ in edges[1:]] + [len(blocks)]
    return [(title, list(blocks[i:end])) for (i, title), end in zip(edges, bounds, strict=True) if blocks[i:end]]


def _merge_small(groups: Sequence[tuple[str, list[Block]]]) -> list[tuple[str, list[Block]]]:
    merged: list[tuple[str, list[Block]]] = []
    carried: tuple[str, list[Block]] | None = None
    for title, blocks in groups:
        if carried:
            title, blocks = carried[0], carried[1] + blocks
            carried = None
        if len(_join(blocks)) < MIN_SECTION_CHARS:
            carried = (title, blocks)
        else:
            merged.append((title, blocks))
    if carried:
        if merged:
            merged[-1] = (merged[-1][0], merged[-1][1] + carried[1])
        else:
            merged.append(carried)
    return merged


def _split(blocks: Sequence[Block]) -> list[list[Block]]:
    """Parts within the size limit, preferring to break at a heading. A table is never cut."""
    parts: list[list[Block]] = []
    current: list[Block] = []
    size = 0
    for block in blocks:
        full = size + len(block.text) + 2 > MAX_SECTION_CHARS
        if current and (full or (block.heading and size >= MAX_SECTION_CHARS / 2)):
            parts.append(current)
            current, size = [], 0
        current.append(block)
        size += len(block.text) + 2
    return [*parts, current] if current else parts


def extract_pdf(data: bytes) -> ExtractedDocument:
    pages = read_pages(data)
    front = "\n".join(page.text for page in pages[:FRONT_PAGES])
    annual, monthly = front_dates(front)
    contents, entries = _find_contents(_without_margin_lines(pages))  # "Page 2 of 6" looks like an entry
    body_pages = pages[max(contents) + 1 :] if contents else pages
    blocks = _blocks(body_pages)
    starts = _locate(blocks, [title for title, _ in entries]) if entries else _font_starts(blocks)
    groups = _group(blocks, starts)
    if not contents:
        groups = _merge_small(groups)
    sections = [
        ExtractedSection(
            title=title,
            part=number,
            page_start=part[0].page,
            page_end=part[-1].page,
            text=_join(part),
        )
        for title, group in groups
        for number, part in enumerate(_split(group), start=1)
    ]
    return ExtractedDocument(
        pages=len(pages),
        name=front_name(front),
        annual_review_date=annual,
        monthly_revision_date=monthly,
        method="contents" if entries else "font",
        contents_entries=len(entries),
        contents_located=len(starts) if entries else 0,
        tables=sum(block.table for block in blocks),
        skipped_pages=len(pages) - len(body_pages),
        sections=sections,
    )
