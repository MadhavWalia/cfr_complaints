"""Procedure documents (LDR-0019).

    python -m cfr_v2.procedures inspect /path/to/pdfs
    python -m cfr_v2.procedures inspect /path/to/pdfs --out inspect.json

``inspect`` prints file names, counts and yes/no flags only, never document text, so the
output can be shared.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .inspect import FRONT_LABELS, DocumentReport, inspect_pdf


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.procedures", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("inspect", help="numbers-only report on a folder of PDFs")
    cmd.add_argument("folder", type=Path)
    cmd.add_argument("--out", type=Path, help="also write the report as JSON")
    args = parser.parse_args(argv)

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
