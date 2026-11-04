"""Procedure documents (LDR-0019).

    python -m cfr_v2.procedures inspect /path/to/pdfs
    python -m cfr_v2.procedures inspect /path/to/pdfs --out inspect.json
    python -m cfr_v2.procedures extract /path/to/pdfs --out extract_trial
    python -m cfr_v2.procedures snapshot /path/to/pdfs [--previous sha256:...]
    python -m cfr_v2.procedures snapshot --confluence

``inspect`` and the printed output of ``extract`` and ``snapshot`` carry file names, dates, counts
and flags only, never document text, so they can be shared. ``extract`` is a trial: it stores
nothing, and ``--out`` writes the redacted text for reading locally. **Those files are document
text: keep them local.** ``snapshot`` stores the originals and the redacted sections.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cfr_v2.contracts import ProcedureDocument, ProcedureStatus

from .corpus import SnapshotResult, build_snapshot, load_snapshot
from .extract import MAX_SECTION_CHARS
from .inspect import FRONT_LABELS, DocumentReport, inspect_pdf
from .sources import ConfluenceSettings, ConfluenceSource, ManualPdfSource, ProcedureSource, ProcedureSourceError

MAX_PDF_BYTES = 200 * 1024 * 1024  # procedure PDFs with images are larger than case artifacts


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.0f}%"


def print_report(reports: list[DocumentReport]) -> None:
    header = (
        "pages",
        "chars/pg",
        "low-text",
        "tables",
        "real tbl",
        "images",
        "real img",
        "front",
        "toc items",
        "toc near",
        "toc any",
        "font hdg",
        "repeated",
    )
    print(f"{'document':<46}" + "".join(f"{h:>10}" for h in header))
    for r in reports:
        if r.status != "ok":
            print(f"{r.document[:45]:<46}  could not read: {r.status}")
            continue
        cells = (
            r.pages,
            r.chars_per_page,
            r.low_text_pages,
            r.tables,
            r.content_tables,
            r.images,
            r.content_images,
            f"{sum(r.front_fields.values())}/{len(FRONT_LABELS)}",
            r.contents_entries,
            _pct(r.contents_match),
            _pct(r.contents_anywhere_match),
            r.font_headings,
            r.repeated_lines,
        )
        print(f"{r.document[:45]:<46}" + "".join(f"{c:>10}" for c in cells))
    read = [r for r in reports if r.status == "ok"]
    unique = {r.sha256: r for r in read}
    pages = sum(r.pages for r in unique.values())
    print(f"\n{len(read)} of {len(reports)} files read; {len(unique)} distinct by content")
    print(f"distinct: {pages:,} pages; {sum(r.chars for r in unique.values()):,} characters")
    if pages:
        distinct = unique.values()
        low = sum(r.low_text_pages for r in distinct)
        print(f"low-text pages: {low} ({_pct(low / pages)})")
        print(f"tables: {sum(r.tables for r in distinct)}, real {sum(r.content_tables for r in distinct)}")
        print(
            f"images: {sum(r.images for r in distinct)}, real {sum(r.content_images for r in distinct)} "
            f"on {sum(r.pages_with_content_images for r in distinct)} pages"
        )
        for name in FRONT_LABELS:
            print(f"first pages have {name}: {sum(r.front_fields.get(name, False) for r in distinct)} of {len(unique)}")
        entries = sum(r.contents_entries for r in distinct)
        if entries:
            near = sum(r.contents_found for r in distinct) / entries
            anywhere = sum(r.contents_anywhere for r in distinct) / entries
            print(f"contents entries ({entries}): near their page {_pct(near)}; anywhere {_pct(anywhere)}")
    groups: dict[str, list[str]] = {}
    for r in read:
        groups.setdefault(r.sha256, []).append(r.document)
    copies = [names for names in groups.values() if len(names) > 1]
    if copies:
        print("\nidentical files:")
        for names in copies:
            print("  " + " = ".join(names))
    same_size: dict[tuple[int, int], set[str]] = {}
    for r in unique.values():
        same_size.setdefault((r.pages, r.chars), set()).add(r.document)
    lookalikes = [sorted(names) for names in same_size.values() if len(names) > 1]
    if lookalikes:
        print("\nsame size, different files (versions?):")
        for names in lookalikes:
            print("  " + " | ".join(names))


def _progress(number: int, total: int, document) -> None:
    print(f"[{number}/{total}] {document.name}", file=sys.stderr)


def _date(value) -> str:
    return value.isoformat() if value else "-"


def print_snapshot(result: SnapshotResult) -> None:
    header = ("status", "annual", "monthly", "file date", "pages", "method", "located", "sections", "largest", "over", "tables", "masked")
    widths = (12, 12, 12, 12, 7, 10, 9, 10, 9, 6, 8, 8)
    print(f"{'document':<46}" + "".join(f"{h:>{w}}" for h, w in zip(header, widths, strict=True)))
    for entry in result.snapshot.entries:
        d = result.documents[entry.source_sha256]
        q = d.quality
        located = _pct(q.contents_located / q.contents_entries) if q.contents_entries else "-"
        cells = (
            entry.status.value[:10],
            _date(d.annual_review_date),
            _date(d.monthly_revision_date),
            _date(d.file_date),
            q.pages,
            q.method,
            located,
            len(d.sections),
            max((len(s.text) for s in d.sections), default=0),
            q.oversized_sections,
            q.tables,
            sum(s.pii_masked for s in d.sections),
        )
        print(f"{d.file_name[:45]:<46}" + "".join(f"{c:>{w}}" for c, w in zip(cells, widths, strict=True)))
    entries = result.snapshot.entries
    current = [result.documents[e.source_sha256] for e in entries if e.status == ProcedureStatus.CURRENT]
    sections = [s for d in current for s in d.sections]
    print(f"\n{len(entries)} distinct files, {len(current)} procedures; {result.reused} reused from the previous snapshot")
    print(f"current: {sum(d.quality.pages for d in current):,} pages; {len(sections):,} sections; "
          f"{sum(len(s.text) for s in sections):,} characters")
    print(f"sections over {MAX_SECTION_CHARS:,} characters: {sum(d.quality.oversized_sections for d in current)}")
    print(f"names taken from the file name: {sum(d.name_from_file for d in current)}")
    print(f"no monthly date: {sum(d.monthly_revision_date is None for d in current)}; "
          f"no annual date: {sum(d.annual_review_date is None for d in current)}")
    print(f"PII masks: {sum(s.pii_masked for s in sections):,}")
    groups: dict[str, list] = {}
    for entry in entries:
        groups.setdefault(entry.procedure_key, []).append(entry)
    versions = [group for group in groups.values() if len(group) > 1]
    if versions:
        print("\nversions (first one is used):")
        for group in versions:
            ordered = sorted(group, key=lambda e: e.status != ProcedureStatus.CURRENT)
            print("  " + " > ".join(e.file_names[0] for e in ordered))
    review = [e for e in entries if e.flags]
    if review:
        print("\nplease check the version chosen:")
        for entry in review:
            print(f"  {entry.file_names[0]}: {', '.join(f.value for f in entry.flags)}")
    copies = [e.file_names for e in entries if len(e.file_names) > 1]
    if copies:
        print("\nidentical files:")
        for names in copies:
            print("  " + " = ".join(names))
    if result.snapshot.failures:
        print("\ncould not prepare (left out):")
        for failure in result.snapshot.failures:
            print(f"  {failure.file_name}: {failure.error}")


def _as_markdown(document: ProcedureDocument) -> str:
    parts = [f"# {document.name}\n"]
    for s in document.sections:
        part = f" (part {s.part})" if s.part > 1 else ""
        pages = f"page {s.page_start}" if s.page_start == s.page_end else f"pages {s.page_start}-{s.page_end}"
        parts.append(f"## {s.title}{part}\n\n_{pages}_\n\n{s.text}\n")
    return "\n".join(parts)


def write_review(result: SnapshotResult, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for document in result.documents.values():
        name = f"{Path(document.file_name).stem}.md"
        (folder / name).write_text(_as_markdown(document), encoding="utf-8")


def _source(args: argparse.Namespace) -> ProcedureSource:
    return ConfluenceSource(ConfluenceSettings.from_env()) if args.confluence else ManualPdfSource(args.folder)


def _masker():
    from cfr_v2.intake.redaction import EEUtilsMasker

    return EEUtilsMasker()


def run_extract(args: argparse.Namespace) -> int:
    from cfr_v2.artifacts import InMemoryArtifactStore

    store = InMemoryArtifactStore(max_bytes=MAX_PDF_BYTES)
    result = build_snapshot(_source(args), store=store, masker=_masker(), progress=_progress)
    print_snapshot(result)
    if args.out:
        write_review(result, args.out)
        print(f"\nredacted text written to {args.out}: keep it local")
    return 1 if result.snapshot.failures else 0


def run_snapshot(args: argparse.Namespace) -> int:
    from cfr_v2.artifacts import S3ArtifactStore, S3Settings, register
    from cfr_v2.runtime import RuntimeSettings
    from cfr_v2.state import CaseStateRepository, Database

    store = S3ArtifactStore(S3Settings.from_env(), max_bytes=MAX_PDF_BYTES)
    previous = load_snapshot(store, args.previous) if args.previous else None
    result = build_snapshot(_source(args), store=store, masker=_masker(), previous=previous, progress=_progress)
    db = Database(RuntimeSettings.from_env().state_database_url, max_connections=2)
    with db.transaction() as tx:
        repo = CaseStateRepository(tx)
        for stored in result.stored:
            register(repo, stored)
    print_snapshot(result)
    print(f"\nsnapshot: {result.ref}")
    return 1 if result.snapshot.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.procedures", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("inspect", help="numbers-only report on a folder of PDFs")
    cmd.add_argument("folder", type=Path)
    cmd.add_argument("--out", type=Path, help="also write the report as JSON")
    for name, text in (("extract", "trial: store nothing"), ("snapshot", "store originals and redacted sections")):
        step = sub.add_parser(name, help=text)
        step.add_argument("folder", type=Path, nargs="?")
        step.add_argument("--confluence", action="store_true", help="read from Confluence (not built yet)")
        if name == "extract":
            step.add_argument("--out", type=Path, help="write the redacted text here, for local reading")
        else:
            step.add_argument("--previous", help="snapshot ref: unchanged PDFs are not read again")
    args = parser.parse_args(argv)
    if args.command != "inspect":
        if not args.folder and not args.confluence:
            parser.error("give a folder of PDFs, or --confluence")
        try:
            return run_extract(args) if args.command == "extract" else run_snapshot(args)
        except ProcedureSourceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    paths = sorted(p for p in args.folder.iterdir() if p.suffix.lower() == ".pdf") if args.folder.is_dir() else []
    if not paths:
        print(f"error: no PDF files in {args.folder}", file=sys.stderr)
        return 1
    reports = []
    for i, path in enumerate(paths, start=1):
        print(f"[{i}/{len(paths)}] {path.name}", file=sys.stderr)
        reports.append(inspect_pdf(path))
    print_report(reports)
    if args.out:
        args.out.write_text(json.dumps([r.as_dict() for r in reports], indent=2) + "\n")
        print(f"\nreport written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
