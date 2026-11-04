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

from .inspect import PAGE_ONE_LABELS, DocumentReport, inspect_pdf


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.0f}%"


def print_report(reports: list[DocumentReport]) -> None:
    header = (
        "pages",
        "chars/pg",
        "low-text",
        "tables",
        "images",
        "pg1",
        "toc pgs",
        "toc items",
        "toc match",
        "repeated",
    )
    print(f"{'document':<46}" + "".join(f"{h:>10}" for h in header))
    for r in reports:
        if r.status != "ok":
            print(f"{r.document[:45]:<46}  could not read: {r.status}")
            continue
        found = sum(r.page_one_fields.values())
        cells = (
            r.pages,
            r.chars_per_page,
            r.low_text_pages,
            r.tables,
            r.images,
            f"{found}/{len(PAGE_ONE_LABELS)}",
            r.contents_pages,
            r.contents_entries,
            _pct(r.contents_match),
            r.repeated_lines,
        )
        print(f"{r.document[:45]:<46}" + "".join(f"{c:>10}" for c in cells))
    read = [r for r in reports if r.status == "ok"]
    pages = sum(r.pages for r in read)
    print(f"\n{len(read)} of {len(reports)} documents read; {pages:,} pages; {sum(r.chars for r in read):,} characters")
    if pages:
        low = sum(r.low_text_pages for r in read)
        print(
            f"low-text pages: {low} ({_pct(low / pages)}); tables: {sum(r.tables for r in read)}; images: {sum(r.images for r in read)}"
        )
        for name in PAGE_ONE_LABELS:
            print(f"page 1 has {name}: {sum(r.page_one_fields.get(name, False) for r in read)} of {len(read)}")
    entries = sum(r.contents_entries for r in read)
    if entries:
        print(
            f"contents entries found near their page: {_pct(sum(r.contents_found for r in read) / entries)} of {entries}"
        )


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
