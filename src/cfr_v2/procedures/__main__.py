"""Procedure documents (LDR-0019).

    python -m cfr_v2.procedures inspect /path/to/pdfs
    python -m cfr_v2.procedures inspect /path/to/pdfs --out inspect.json
    python -m cfr_v2.procedures extract /path/to/pdfs --out extract_trial
    python -m cfr_v2.procedures snapshot /path/to/pdfs [--previous sha256:...]
    python -m cfr_v2.procedures snapshot --confluence
    python -m cfr_v2.procedures index sha256:<snapshot>
    python -m cfr_v2.procedures search sha256:<index> "fee charged after the card was cancelled"
    python -m cfr_v2.procedures lightrag-build sha256:<index> --workdir lightrag_store
    python -m cfr_v2.procedures bakeoff sha256:<index> --workdir lightrag_store --claims-from c02_trial --out bakeoff
    python -m cfr_v2.procedures bakeoff-score bakeoff/judgements.xlsx bakeoff/key.json

``inspect`` and the printed output of ``extract`` and ``snapshot`` carry file names, dates, counts
and flags only, never document text, so they can be shared. ``extract`` is a trial: it stores
nothing, and ``--out`` writes the redacted text for reading locally. **Those files are document
text: keep them local.** ``snapshot`` stores the originals and the redacted sections. ``index``
embeds the current sections of a snapshot through the gateway. ``search`` is for trying queries:
**it prints section titles, and passages with ``--text``, so keep its output local too.**
``lightrag-build`` builds the challenger from the same passages: two gpt-5 calls per passage, and
it can resume. ``bakeoff`` searches with both engines and writes a blind workbook and a separate
key: **the workbook holds claim and procedure text, so keep it local.** ``bakeoff-score`` prints
numbers only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cfr_v2.contracts import ProcedureDocument, ProcedureStatus

from .corpus import SnapshotResult, build_snapshot, load_snapshot, same_name_different_files
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
    header = (
        "status",
        "annual",
        "monthly",
        "file date",
        "pages",
        "method",
        "located",
        "sections",
        "largest",
        "over",
        "tables",
        "masked",
    )
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
    print(
        f"\n{len(entries)} distinct files, {len(current)} procedures; {result.reused} reused from the previous snapshot"
    )
    print(
        f"current: {sum(d.quality.pages for d in current):,} pages; {len(sections):,} sections; "
        f"{sum(len(s.text) for s in sections):,} characters"
    )
    print(f"sections over {MAX_SECTION_CHARS:,} characters: {sum(d.quality.oversized_sections for d in current)}")
    print(f"names taken from the file name: {sum(d.name_from_file for d in current)}")
    print(
        f"no monthly date: {sum(d.monthly_revision_date is None for d in current)}; "
        f"no annual date: {sum(d.annual_review_date is None for d in current)}"
    )
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
    lookalikes = same_name_different_files(result.documents.values())
    if lookalikes:
        print("\nsame front-page name, different file names (kept apart, please check):")
        for names in lookalikes:
            print("  " + " | ".join(names))
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
    store = _store()
    previous = load_snapshot(store, args.previous) if args.previous else None
    result = build_snapshot(_source(args), store=store, masker=_masker(), previous=previous, progress=_progress)
    _register(result.stored)
    print_snapshot(result)
    print(f"\nsnapshot: {result.ref}")
    return 1 if result.snapshot.failures else 0


def _store():
    from cfr_v2.artifacts import S3ArtifactStore, S3Settings

    return S3ArtifactStore(S3Settings.from_env(), max_bytes=MAX_PDF_BYTES)


def _embedder(settings=None):
    from cfr_v2.llm import GatewayEmbeddingClient

    return GatewayEmbeddingClient(settings)


def _build_embedder():
    from cfr_v2.llm import EmbeddingSettings

    from .lightrag_engine import build_embedding_settings

    return _embedder(build_embedding_settings(EmbeddingSettings.from_env()))


def _register(stored: list) -> None:
    from cfr_v2.artifacts import register
    from cfr_v2.runtime import RuntimeSettings
    from cfr_v2.state import CaseStateRepository, Database

    db = Database(RuntimeSettings.from_env().state_database_url, max_connections=2)
    with db.transaction() as tx:
        repo = CaseStateRepository(tx)
        for item in stored:
            register(repo, item)


def run_index(args: argparse.Namespace) -> int:
    from cfr_v2.llm import model_loop

    from .search import build_index

    store = _store()
    build = model_loop().run(build_index(store, args.snapshot, _embedder()))
    _register(build.stored)
    print(f"{build.procedures} procedures, {build.passages} passages")
    print(f"blocked by the gateway (keyword search only): {build.blocked}")
    print(f"index: {build.ref}")
    return 0


def print_hits(result, *, show_text: bool) -> None:
    if not result.vector_search:
        print("the gateway blocked the query: keyword results only")
    print(f"{'#':>3} {'score':>8} {'vec':>4} {'key':>4} {'pages':>9}  document / section")
    for number, hit in enumerate(result.hits, start=1):
        pages = str(hit.page_start) if hit.page_start == hit.page_end else f"{hit.page_start}-{hit.page_end}"
        print(
            f"{number:>3} {hit.score:>8.4f} {hit.vector_rank or '-':>4} {hit.keyword_rank or '-':>4} {pages:>9}"
            f"  {hit.file_name} / {hit.section_title}"
        )
        if show_text:
            print(f"\n{hit.text}\n")


def run_search(args: argparse.Namespace) -> int:
    from cfr_v2.contracts import ProcedureSearchRequest
    from cfr_v2.llm import model_loop

    from .search import HybridProcedureSearch

    search = HybridProcedureSearch.load(_store(), args.index, _embedder())
    request = ProcedureSearchRequest(query=_masker().mask(args.query), limit=args.limit, redacted=True)
    print_hits(model_loop().run(search.search(request)), show_text=args.text)
    return 0


def _chat_client():
    from cfr_v2.llm import GatewayModelClient

    return GatewayModelClient()


def run_lightrag_build(args: argparse.Namespace) -> int:
    from cfr_v2.llm import model_loop

    from .lightrag_engine import build_lightrag, index_passages

    passages = index_passages(_store(), args.index)
    build = model_loop().run(
        build_lightrag(args.workdir, passages, _build_embedder(), _chat_client(), concurrency=args.concurrency)
    )
    print(f"{build.processed} of {build.passages} passages in the graph; {build.failed} failed")
    print(
        f"model calls this run: {build.model_calls}; blocked by the gateway: "
        f"{build.blocked_calls} model calls, {build.blocked_embeddings} embeddings"
    )
    if build.duplicates:
        print(f"{build.duplicates} passages with identical text went in once")
    if build.stopped:
        print("stopped early: batches in a row added nothing. Check the gateway, then run this again to resume")
    return 1 if build.failed or build.processed < build.passages else 0


async def _collect(args: argparse.Namespace, claims: list):
    from .bakeoff import collect
    from .lightrag_engine import GatewayLlm, LightRagProcedureSearch, index_passages, open_rag
    from .search import HybridProcedureSearch

    store, embedder = _store(), _embedder()
    hybrid = HybridProcedureSearch.load(store, args.index, embedder)
    llm = GatewayLlm(_chat_client())
    async with open_rag(args.workdir, embedder, llm) as rag:
        lightrag = LightRagProcedureSearch(rag, args.index, index_passages(store, args.index), llm)
        return await collect(claims, {"hybrid": hybrid, "lightrag": lightrag}, top=args.top)


def run_bakeoff(args: argparse.Namespace) -> int:
    from cfr_v2.llm import model_loop

    from .bakeoff import load_claims, write_key, write_workbook

    claims = load_claims(args.claims_from, args.claims)
    if len(claims) < args.claims:
        print(f"warning: only {len(claims)} claims in {args.claims_from}", file=sys.stderr)
    if not claims:
        return 1
    items = model_loop().run(_collect(args, claims))
    args.out.mkdir(parents=True, exist_ok=True)
    write_workbook(items, args.out / "judgements.xlsx")
    write_key(items, ("hybrid", "lightrag"), args.top, args.out / "key.json")
    both = sum(len(i.ranks) == 2 for i in items)
    print(f"{len(claims)} claims, {len(items)} passages to judge ({both} found by both engines)")
    print(f"give {args.out / 'judgements.xlsx'} to the judge; keep key.json back. Both stay local")
    return 0


def run_bakeoff_score(args: argparse.Namespace) -> int:
    from .bakeoff import read_judgements, score

    key = json.loads(args.key.read_text(encoding="utf-8"))
    report = score(read_judgements(args.workbook), key)
    print(f"judged {report.judged}; not judged {report.unjudged}; rows not in the key {report.unknown_items}")
    header = ("claims", "any Yes", "Yes share", "Yes+Partly", "first Yes", "only here", "procedures")
    print(f"{'engine':<12}" + "".join(f"{h:>12}" for h in header))
    for s in report.scores:
        cells = (
            s.claims,
            _pct(s.hit_rate),
            _pct(s.precision),
            _pct(s.partly_or_better),
            f"{s.first_relevant:.2f}",
            s.only_here,
            f"{s.procedures_per_claim:.1f}",
        )
        print(f"{s.engine:<12}" + "".join(f"{c:>12}" for c in cells))
    return 0


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
    step = sub.add_parser("index", help="embed the current sections of a snapshot")
    step.add_argument("snapshot", help="snapshot ref")
    step = sub.add_parser("search", help="try a query against an index")
    step.add_argument("index", help="index ref")
    step.add_argument("query")
    step.add_argument("--limit", type=int, default=8)
    step.add_argument("--text", action="store_true", help="print the passages too")
    step = sub.add_parser("lightrag-build", help="build the LightRAG graph and vectors from an index's passages")
    step.add_argument("index", help="index ref")
    step.add_argument("--workdir", type=Path, required=True)
    step.add_argument("--concurrency", type=int, default=16, help="passages processed at once (default 16)")
    step = sub.add_parser("bakeoff", help="search with both engines and write the blind workbook")
    step.add_argument("index", help="index ref")
    step.add_argument("--workdir", type=Path, required=True, help="the LightRAG working directory")
    step.add_argument("--claims-from", type=Path, required=True, help="folder of C02 trial review files")
    step.add_argument("--claims", type=int, default=50, help="claims to use (default 50)")
    step.add_argument("--top", type=int, default=5, help="passages per engine per claim (default 5)")
    step.add_argument("--out", type=Path, required=True)
    step = sub.add_parser("bakeoff-score", help="score a judged workbook")
    step.add_argument("workbook", type=Path)
    step.add_argument("key", type=Path)
    args = parser.parse_args(argv)
    if args.command == "bakeoff-score":
        return run_bakeoff_score(args)
    runners = {"index": run_index, "search": run_search, "lightrag-build": run_lightrag_build, "bakeoff": run_bakeoff}
    if args.command in runners:
        from cfr_v2.llm import close_model_loop
        from cfr_v2.telemetry import init_telemetry, shutdown_telemetry

        init_telemetry(service_name="cfr-v2-procedures")
        try:
            return runners[args.command](args)
        finally:
            close_model_loop()
            shutdown_telemetry()
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
