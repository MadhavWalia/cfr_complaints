"""Procedure documents (LDR-0019).

    python -m cfr_v2.procedures inspect /path/to/pdfs
    python -m cfr_v2.procedures inspect /path/to/pdfs --out inspect.json
    python -m cfr_v2.procedures extract /path/to/pdfs --out extract_trial
    python -m cfr_v2.procedures snapshot /path/to/pdfs [--previous sha256:...]
    python -m cfr_v2.procedures snapshot --confluence
    python -m cfr_v2.procedures snapshot --add-to sha256:<previous> new1.pdf new2.pdf [--leave-out KEY]
    python -m cfr_v2.procedures lightrag-build sha256:<snapshot>
    python -m cfr_v2.procedures lightrag-append sha256:<new> --from sha256:<old> --count-only
    python -m cfr_v2.procedures lightrag-append sha256:<new> --from sha256:<old> --in-place [--backup]
    python -m cfr_v2.procedures stores
    python -m cfr_v2.procedures remove-store lightrag_store_<hash> --yes
    python -m cfr_v2.procedures search sha256:<snapshot> "fee charged after the card was cancelled"

``inspect`` and the printed output of ``extract`` and ``snapshot`` carry file names, dates, counts
and flags only, never document text, so they can be shared. ``extract`` is a trial: it stores
nothing, and ``--out`` writes the redacted text for reading locally. **Those files are document
text: keep them local.** ``snapshot`` stores the originals and the redacted sections.
``lightrag-build`` builds the LightRAG graph and vectors from the snapshot's current procedures:
about three gpt-5 calls per passage, and it can resume. It saves the store every 64 passages
(``--save-every``): a save rewrites files of a gigabyte or more, so saving after every passage fills
a disk whose scanner keeps the old copies open (LDR-0030). Stop (SIGTERM, Ctrl+C) saves the batch in
progress before ending; a crash redoes the passages since the last save. Each snapshot gets its own store,
``lightrag_store_<first 12 of its hash>`` (``stores.py``), where the worker looks for it;
``--workdir`` puts it elsewhere. A ref from the ``index`` command this replaced also works.
``search`` is for trying queries against that store: **it prints section
titles, and passages with ``--text``, so keep its output local too.**

Adding documents (LDR-0043): ``snapshot --add-to`` makes a snapshot of the previous one's documents
plus the PDFs given, choosing each procedure's current version again. ``lightrag-append`` then
moves the previous snapshot's store to the new one **in place**: it deletes the passages the new
snapshot no longer holds (``--keep-gone`` keeps them), inserts the new ones, and renames the
folder after the new snapshot. ``--count-only`` previews it with no model calls. While it runs,
nothing can search the store, so it refuses to start while case runs are queued or running.
``stores`` lists the stores here; ``remove-store`` deletes one that no release pins.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from cfr_v2.contracts import ProcedureDocument, ProcedureStatus

from .corpus import SnapshotResult, build_snapshot, load_snapshot, same_name_different_files
from .extract import MAX_SECTION_CHARS
from .inspect import FRONT_LABELS, DocumentReport, inspect_pdf
from .sources import (
    ChosenPdfSource,
    ConfluenceSettings,
    ConfluenceSource,
    ManualPdfSource,
    ProcedureSource,
    ProcedureSourceError,
)
from .stores import WORKDIR_ENV, list_stores, override_hint, removable, store_dir

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
    if args.confluence:
        return ConfluenceSource(ConfluenceSettings.from_env())
    if getattr(args, "add_to", None):
        return ChosenPdfSource(args.paths, dated=not args.undated)
    return ManualPdfSource(args.folder)


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
    base = load_snapshot(store, args.add_to) if args.add_to else None
    try:
        result = build_snapshot(
            _source(args),
            store=store,
            masker=_masker(),
            previous=previous,
            progress=_progress,
            base=base,
            leave_out=args.leave_out or (),
        )
    except ValueError as exc:  # a name to leave out that matches nothing: checked before anything is read
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _register(result.stored)
    print_snapshot(result)
    if result.added is not None:
        print_added(result)
    print(f"\nsnapshot: {result.ref}")
    return 1 if result.snapshot.failures else 0


def print_added(result: SnapshotResult) -> None:
    added = result.added
    print(
        f"\nadded {len(added.added)} · new versions of {len(added.new_versions)} · "
        f"already in {len(added.already_in)} · left out {len(added.left_out)}"
    )
    for label, names in (
        ("new versions (the newer one is current; please check)", added.new_versions),
        ("already in the snapshot (nothing changed)", added.already_in),
        ("left out", added.left_out),
        ("version decided by the file's date or a tie (please check)", added.dated_by_file),
    ):
        if names:
            print(f"{label}:")
            for name in names:
                print(f"  {name}")


def _delete_given(paths: list[Path]) -> None:
    """The console's uploads: procedure content, kept nowhere but the snapshot's artifacts."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"warning: could not delete {path.name}: {type(exc).__name__}", file=sys.stderr)
    print(f"deleted the {len(paths)} given PDF file(s)")


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


def print_hits(result, *, show_text: bool) -> None:
    if result.blocked:
        print("the gateway blocked part of the search, so it may have found less")
    print(f"{'#':>3} {'pages':>9}  document / section")
    for number, hit in enumerate(result.hits, start=1):
        pages = str(hit.page_start) if hit.page_start == hit.page_end else f"{hit.page_start}-{hit.page_end}"
        print(f"{number:>3} {pages:>9}  {hit.file_name} / {hit.section_title}")
        if show_text:
            print(f"\n{hit.text}\n")


def run_search(args: argparse.Namespace) -> int:
    from cfr_v2.contracts import ProcedureSearchRequest
    from cfr_v2.llm import model_loop

    from .lightrag_engine import GatewayLlm, LightRagProcedureSearch, StoreMismatchError, check_store, open_rag
    from .search import corpus_passages

    workdir = args.workdir or store_dir(args.procedures)
    try:
        check_store(workdir, args.procedures)
    except StoreMismatchError as exc:
        print(f"error: {exc}{'' if args.workdir else override_hint(args.procedures)}", file=sys.stderr)
        return 2
    passages = corpus_passages(_store(), args.procedures)
    request = ProcedureSearchRequest(query=_masker().mask(args.query), limit=args.limit, redacted=True)

    async def search():
        llm = GatewayLlm(_chat_client())
        async with open_rag(workdir, _embedder(), llm) as rag:
            return await LightRagProcedureSearch(rag, args.procedures, passages, llm).search(request)

    print_hits(model_loop().run(search()), show_text=args.text)
    return 0


def _chat_client():
    from cfr_v2.llm import GatewayModelClient

    return GatewayModelClient()


def run_lightrag_build(args: argparse.Namespace) -> int:
    from cfr_v2.llm import model_loop

    from .lightrag_engine import SAVE_EVERY, SaveError, StoreMismatchError, build_lightrag
    from .search import corpus_passages

    save_every = args.save_every or SAVE_EVERY
    if save_every < 1:
        print("error: --save-every: at least 1", file=sys.stderr)
        return 2
    workdir = args.workdir or store_dir(args.procedures)
    print(f"store: {workdir}")
    passages = corpus_passages(_store(), args.procedures)
    stop = threading.Event()

    def ask_to_stop(signum: int, _frame: object) -> None:
        """Stop (SIGTERM) or Ctrl+C: finish and save the batch in progress. A second one: now."""
        if stop.is_set():
            raise SystemExit(128 + signum)
        stop.set()
        print("stopping: the batch in progress is saved first. Stop again to end now, losing it", flush=True)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, ask_to_stop)
    try:
        build = model_loop().run(
            build_lightrag(
                workdir,
                passages,
                _build_embedder(),
                _chat_client(),
                concurrency=args.concurrency,
                procedures_ref=args.procedures,
                save_every=save_every,
                stop=stop,
            )
        )
    except StoreMismatchError as exc:
        print(f"error: {exc}{'' if args.workdir else override_hint(args.procedures)}", file=sys.stderr)
        return 2
    except SaveError as exc:
        print(f"error: {exc}. Run this again to resume from the last save", file=sys.stderr)
        return 1
    print(f"{build.processed} of {build.passages} passages in the graph; {build.failed} failed")
    print(
        f"model calls this run: {build.model_calls}; blocked by the gateway: "
        f"{build.blocked_calls} model calls, {build.blocked_embeddings} embeddings"
    )
    if build.duplicates:
        print(f"{build.duplicates} passages with identical text went in once")
    if build.stopped:
        print("stopped early: batches in a row added nothing. Check the gateway, then run this again to resume")
    if build.asked_to_stop:
        print("stopped as asked, with the batch in progress saved. Run this again to resume")
        return 0
    return 1 if build.failed or build.processed < build.passages else 0


IN_PLACE_WARNING = (
    "This changes the store the current release uses. While it runs, and until a release pins the new "
    "snapshot, routing can't search procedures. Stop the worker first: this refuses to start while case runs "
    "are queued or running. Afterwards the store holds the new snapshot only: going back means rebuilding the "
    "old one, unless --backup keeps a copy."
)
QUEUE_SQL = (
    "SELECT count(*) FILTER (WHERE status = 'QUEUED'), count(*) FILTER (WHERE status = 'RUNNING') FROM workflow_job"
)


def queue_busy() -> str | None:
    """Why routing may need the store now: runs queued or held by a worker. A queue that can't be
    read counts as busy: this is the check that keeps a worker from failing runs at C04."""
    try:
        from cfr_v2.runtime import RuntimeSettings
        from cfr_v2.state import Database

        db = Database(RuntimeSettings.from_env().state_database_url, max_connections=1)
        try:
            with db.transaction() as tx:
                queued, running = tx.one(QUEUE_SQL) or (0, 0)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - the type only: a message can name the host
        return f"the queue can't be checked ({type(exc).__name__})"
    if running:
        return f"{running} case run(s) are running: stop the worker first"
    if queued:
        return f"{queued} case run(s) are queued: a worker started now would fail them at routing. Cancel them or wait"
    return None


def _gb(size: int) -> str:
    return f"{size / 1e9:.2f} GB"


def _already_appended(target: Path, from_ref: str, to_ref: str) -> bool:
    from .stores import COMPLETE, read_marker, state

    marker = read_marker(target) or {}
    last = (marker.get("lineage") or [{}])[-1]
    return (
        marker.get("procedures_ref") == to_ref
        and state(marker) == COMPLETE
        and last.get("made") == "append"
        and last.get("from") == from_ref
    )


def run_lightrag_append(args: argparse.Namespace) -> int:
    from cfr_v2.llm import model_loop

    from .append import (
        AppendRefused,
        append_lightrag,
        backup_store,
        check_base,
        finish,
        free_space_needed,
        mark_appending,
        plan_append,
        read_store,
    )
    from .lightrag_engine import SAVE_EVERY, SaveError
    from .search import corpus_passages

    save_every = args.save_every or SAVE_EVERY
    if save_every < 1:
        print("error: --save-every: at least 1", file=sys.stderr)
        return 2
    workdir = args.workdir or store_dir(args.from_ref)
    target = store_dir(args.procedures)
    rename = args.workdir is None and not os.environ.get(WORKDIR_ENV)
    if rename and not workdir.exists() and _already_appended(target, args.from_ref, args.procedures):
        print(f"already appended: {target} holds {args.procedures}")
        return 0
    embedder = _build_embedder()
    try:
        if not workdir.is_dir():
            raise AppendRefused(f"no LightRAG store at {workdir}{'' if args.workdir else override_hint(args.from_ref)}")
        contents = read_store(workdir)
        marker, notes = check_base(workdir, args.from_ref, args.procedures, embedder, contents)
        if rename and target.exists() and target.resolve() != workdir.resolve():
            raise AppendRefused(f"{target} already exists: remove it, or append with --workdir")
        passages = corpus_passages(_store(), args.procedures)
        if not passages:
            raise AppendRefused(f"{args.procedures} has no current procedures")
    except AppendRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    plan = plan_append(contents, passages)
    need, free = free_space_needed(workdir, backup=args.backup), shutil.disk_usage(workdir).free
    print(f"store: {workdir} (holds {args.from_ref}, {len(contents.processed):,} passages)")
    print(f"to: {args.procedures}" + (f", renamed to {target.name} when done" if rename else ""))
    counts = plan.counts()
    deletes = len(plan.deletes(keep_gone=args.keep_gone))
    print(
        f"unchanged {counts['unchanged']:,} · new {counts['new']:,} · gone {counts['gone']:,} · "
        f"changed {counts['changed']:,}" + (" (gone ones kept: --keep-gone)" if args.keep_gone else "")
    )
    if plan.duplicates:
        print(f"{plan.duplicates} passages with identical text go in once")
    inserting = counts["new"] + counts["changed"]
    print(
        f"estimate: about {3 * inserting:,} gpt-5 calls ({inserting:,} passages to insert, about 3 each) and {deletes:,} deletes"
    )
    print(f"disk: {_gb(need)} needed, {_gb(free)} free")
    for note in notes:
        print(f"note: {note}")
    if args.count_only:
        print(f"preview: {plan.line()}")
        return 0
    print(f"\nwarning: {IN_PLACE_WARNING}", file=sys.stderr)
    if not args.in_place:
        print("error: an append changes the store in place: give --in-place to go ahead", file=sys.stderr)
        return 2
    if free < need:
        print(f"error: not enough disk: {_gb(need)} needed, {_gb(free)} free", file=sys.stderr)
        return 2
    if busy := queue_busy():
        print(f"error: {busy}", file=sys.stderr)
        return 2
    if args.backup:
        copy = workdir.with_name(f"{workdir.name}.before-{datetime.now(UTC):%Y%m%d-%H%M%S}")
        try:
            copied = backup_store(workdir, copy)
        except (AppendRefused, OSError) as exc:
            print(
                f"error: the backup failed ({exc if isinstance(exc, AppendRefused) else type(exc).__name__}); "
                "nothing was changed",
                file=sys.stderr,
            )
            shutil.rmtree(copy, ignore_errors=True)
            return 1
        print(f"backup: {copy} ({_gb(copied)})")
    mark_appending(workdir, marker, args.procedures, plan, args.keep_gone)
    stop = threading.Event()

    def ask_to_stop(signum: int, _frame: object) -> None:
        if stop.is_set():
            raise SystemExit(128 + signum)
        stop.set()
        print("stopping: what's in progress is saved first. Stop again to end now, losing it", flush=True)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, ask_to_stop)
    try:
        outcome = model_loop().run(
            append_lightrag(
                workdir,
                plan,
                embedder,
                _chat_client(),
                keep_gone=args.keep_gone,
                concurrency=args.concurrency,
                save_every=save_every,
                stop=stop,
            )
        )
    except SaveError as exc:
        print(f"error: {exc}. Run this again to resume from the last save", file=sys.stderr)
        return 1
    print(f"deleted {outcome.deleted} of {deletes}; {len(outcome.delete_failed)} could not be")
    build = outcome.build
    if build is not None:
        print(
            f"model calls this run: {build.model_calls}; blocked by the gateway: "
            f"{build.blocked_calls} model calls, {build.blocked_embeddings} embeddings"
        )
    if not outcome.complete:
        if outcome.asked_to_stop:
            print("stopped as asked, with what was in progress saved. Run this again to finish the append")
            return 0
        reason = (
            "some passages could not be deleted"
            if outcome.delete_failed
            else "batches in a row added nothing: check the gateway"
        )
        print(f"the append isn't finished ({reason}); the store stays closed to search. Run this again to resume")
        return 1
    finish(workdir, marker, args.procedures, outcome, embedder)
    if rename and workdir.resolve() != target.resolve():
        workdir.rename(target)
        print(f"renamed {workdir.name} to {target.name}")
    print(f"{build.processed} of {build.passages} passages in the graph; {build.failed} failed")
    if build.failed:
        print(f"run lightrag-build {args.procedures} to retry the failed ones")
    return 1 if build.failed else 0


def run_stores(args: argparse.Namespace) -> int:
    found = list_stores(Path(), os.environ)
    if not found:
        print("no LightRAG stores here")
        return 0
    print(f"{'store':<34}{'snapshot':<22}{'state':<11}{'passages':>10}{'size':>10}{'appends':>9}  pinned by")
    for info in found:
        snapshot = (info.snapshot or "-").removeprefix("sha256:")[:16]
        state = info.state or "no marker"
        passages = "-" if info.passages is None else f"{info.passages:,}"
        print(
            f"{info.folder.name[:33]:<34}{snapshot:<22}{state:<11}{passages:>10}{_gb(info.bytes):>10}"
            f"{info.appends:>9}  {', '.join(info.pinned_by) or '-'}"
        )
    return 0


def run_remove_store(args: argparse.Namespace) -> int:
    folder = args.folder
    info = next(
        (i for i in list_stores(Path(), os.environ, extra=[folder]) if i.folder.resolve() == folder.resolve()), None
    )
    if info is None:
        print(f"error: {folder} is not a LightRAG store: nothing deleted", file=sys.stderr)
        return 2
    if why := removable(info, os.environ, Path()):
        print(f"error: {folder} can't be removed: {why}", file=sys.stderr)
        return 2
    holds = (info.snapshot or "no recorded snapshot").removeprefix("sha256:")[:16]
    print(f"{folder} holds {holds} ({info.state or 'no marker'}), {_gb(info.bytes)}")
    if not args.yes:
        print("nothing deleted: give --yes to delete it for good")
        return 0
    shutil.rmtree(folder)
    print(f"removed {folder.name}: {_gb(info.bytes)} freed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.procedures", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("inspect", help="numbers-only report on a folder of PDFs")
    cmd.add_argument("folder", type=Path)
    cmd.add_argument("--out", type=Path, help="also write the report as JSON")
    for name, text in (("extract", "trial: store nothing"), ("snapshot", "store originals and redacted sections")):
        step = sub.add_parser(name, help=text)
        if name == "extract":
            step.add_argument("folder", type=Path, nargs="?")
        else:
            step.add_argument("paths", type=Path, nargs="*", help="a folder of PDFs; with --add-to, the PDFs to add")
        step.add_argument("--confluence", action="store_true", help="read from Confluence (not built yet)")
        if name == "extract":
            step.add_argument("--out", type=Path, help="write the redacted text here, for local reading")
        else:
            step.add_argument("--previous", help="snapshot ref: unchanged PDFs are not read again")
            step.add_argument("--add-to", help="snapshot ref: a snapshot of its documents plus the PDFs given")
            step.add_argument(
                "--leave-out",
                action="append",
                metavar="KEY",
                help="with --add-to: drop the previous document with this procedure key or file name (repeatable)",
            )
            step.add_argument(
                "--undated", action="store_true", help="the files' own dates mean nothing (uploads): use their names"
            )
            step.add_argument("--delete-after", action="store_true", help="delete the PDFs given when done")
    step = sub.add_parser("lightrag-build", help="build the LightRAG graph and vectors from a snapshot's procedures")
    step.add_argument("procedures", help="snapshot ref (or a ref from the old index command)")
    step.add_argument("--workdir", type=Path, help="the LightRAG store (default: the snapshot's own folder)")
    step.add_argument("--concurrency", type=int, default=16, help="passages processed at once (default 16)")
    step.add_argument(
        "--save-every",
        type=int,
        default=None,
        metavar="N",
        help="save the store every N passages, at the end of a batch (default 64); 1 saves after each one",
    )
    step = sub.add_parser("lightrag-append", help="move a store in place to a later snapshot (LDR-0043)")
    step.add_argument("procedures", help="the new snapshot ref")
    step.add_argument("--from", dest="from_ref", required=True, help="the snapshot the store holds now")
    step.add_argument("--workdir", type=Path, help="the store (default: the --from snapshot's own folder)")
    step.add_argument("--count-only", action="store_true", help="preview: counts, cost and disk; changes nothing")
    step.add_argument("--in-place", action="store_true", help="go ahead: the store changes in place")
    step.add_argument("--keep-gone", action="store_true", help="keep passages the new snapshot doesn't hold")
    step.add_argument("--backup", action="store_true", help="copy the store before changing it")
    step.add_argument("--concurrency", type=int, default=16, help="passages processed at once (default 16)")
    step.add_argument("--save-every", type=int, default=None, metavar="N", help="save every N deletes or passages")
    sub.add_parser("stores", help="list the LightRAG stores here")
    step = sub.add_parser("remove-store", help="delete a store no release pins")
    step.add_argument("folder", type=Path)
    step.add_argument("--yes", action="store_true", help="delete it; otherwise only say what would go")
    step = sub.add_parser("search", help="try a query against a LightRAG store")
    step.add_argument("procedures", help="the ref the store was built from: snapshot, or old index")
    step.add_argument("query")
    step.add_argument("--workdir", type=Path, help="the LightRAG store (default: the snapshot's own folder)")
    step.add_argument("--limit", type=int, default=8)
    step.add_argument("--text", action="store_true", help="print the passages too")
    args = parser.parse_args(argv)
    if args.command == "stores":
        return run_stores(args)
    if args.command == "remove-store":
        return run_remove_store(args)
    runners = {"search": run_search, "lightrag-build": run_lightrag_build, "lightrag-append": run_lightrag_append}
    if args.command in runners:
        from cfr_v2.llm import close_model_loop
        from cfr_v2.telemetry import init_telemetry, shutdown_telemetry

        init_telemetry(service_name="cfr-v2-procedures")
        try:
            return runners[args.command](args)
        finally:
            close_model_loop()
            shutdown_telemetry()
    if args.command == "snapshot":
        if args.add_to:
            if not args.paths:
                parser.error("--add-to: give the PDFs to add")
        elif args.leave_out:
            parser.error("--leave-out needs --add-to")
        elif len(args.paths) > 1:
            parser.error("give one folder of PDFs (or --add-to and the PDFs to add)")
        elif not args.paths and not args.confluence:
            parser.error("give a folder of PDFs, or --confluence")
        args.folder = args.paths[0] if args.paths and not args.add_to else None
        try:
            return run_snapshot(args)
        except ProcedureSourceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            if args.delete_after and args.paths:
                _delete_given([p for p in args.paths if p.is_file()])
    if args.command == "extract":
        if not args.folder and not args.confluence:
            parser.error("give a folder of PDFs, or --confluence")
        try:
            return run_extract(args)
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
