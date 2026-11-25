"""Append to a LightRAG store in place (LDR-0043): move a store from one snapshot to a later one by
changing only the difference, instead of building the later snapshot's store from nothing.

The store is compared with the new snapshot's CURRENT passages (ids ``p-<pdf hash>-<section>``):

- **unchanged**: in the store with the same text: left alone;
- **new**: not in the store: inserted, as a build does;
- **gone**: in the store, not in the snapshot (a superseded version, a document left out, an id now
  a duplicate): deleted, unless asked to keep them;
- **changed**: the same id with other text (the same PDF extracted or redacted differently):
  deleted, then inserted again.

Deletes come first: stale descriptions are gone before new passages merge into the same entities,
and a changed id can't go in again while it is there. LightRAG's ``adelete_by_doc_id`` removes a
passage's chunk and what only it supported, and rebuilds shared entities from cached extractions,
so a delete makes no extraction call; it can make a summary call, and re-embeds what it rebuilds.

Search already ignores passages outside its snapshot (``LightRagProcedureSearch``), so deleting gone
passages is for search quality, not for what gets cited.

The comparison reads LightRAG's JSON files directly (``read_store``): seconds, against minutes to
load the graph, and no LightRAG import, so the preview runs anywhere. It holds the store's text in
memory and never prints it.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cfr_v2.llm import EmbeddingClient, ModelClient
from cfr_v2.telemetry import get_logger

from .lightrag_engine import (
    PROGRAM_VERSION,
    BatchedSaves,
    GatewayLlm,
    LightRagBuild,
    _built,
    _insert_passes,
    _now,
    embedding_of,
    open_rag,
    passage_id,
)
from .search import Passage
from .stores import APPENDING, COMPLETE, read_marker, write_marker
from .stores import state as store_state

logger = get_logger(__name__)

DOC_STATUS = "kv_store_doc_status.json"
FULL_DOCS = "kv_store_full_docs.json"
CHUNK_VECTORS = "vdb_chunks.json"
PROCESSED = "processed"  # LightRAG's DocStatus.PROCESSED, as stored
DELETED = ("success", "not_found")  # a delete's outcomes that leave the passage gone
REBUILD_AFTER = 5  # appends in a store's lineage after which a rebuild is suggested


class AppendRefused(RuntimeError):
    """The append can't start; nothing was changed."""


@dataclass(frozen=True)
class StoreContents:
    processed: dict[str, str | None]  # passage id -> its text (None when LightRAG has no copy of it)
    others: frozenset[str]  # ids with another status: failed, queued, LightRAG's duplicate rows
    dimensions: int | None  # of the vectors stored


def _json(path: Path) -> dict[str, Any]:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise AppendRefused(f"{path.name} in the store can't be read ({type(exc).__name__})") from None
    return found if isinstance(found, dict) else {}


def stored_dimensions(workdir: Path) -> int | None:
    """From the head of the chunk vectors' file: nano-vectordb writes ``embedding_dim`` first."""
    try:
        with (workdir / CHUNK_VECTORS).open("rb") as handle:
            head = handle.read(4096).decode("utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"embedding_dim"\s*:\s*(\d+)', head)
    return int(match.group(1)) if match else None


def read_store(workdir: Path) -> StoreContents:
    """What a store holds, from LightRAG's own files (the default JSON storages)."""
    if not (workdir / DOC_STATUS).is_file():
        raise AppendRefused(f"{workdir} has no {DOC_STATUS}: not a LightRAG store with JSON storages")
    statuses, documents = _json(workdir / DOC_STATUS), _json(workdir / FULL_DOCS)
    processed: dict[str, str | None] = {}
    others: set[str] = set()
    for doc_id, row in statuses.items():
        if isinstance(row, dict) and str(row.get("status", "")).lower() == PROCESSED:
            document = documents.get(doc_id)
            processed[doc_id] = document.get("content") if isinstance(document, dict) else None
        else:
            others.add(doc_id)
    return StoreContents(processed, frozenset(others), stored_dimensions(workdir))


@dataclass(frozen=True)
class AppendPlan:
    passages: tuple[Passage, ...]  # the new snapshot's, one per text: what the store will hold
    unchanged: tuple[str, ...]
    new: tuple[str, ...]
    changed: tuple[str, ...]
    gone: tuple[str, ...]
    duplicates: int  # the new snapshot's passages that repeat another's text and go in once

    def deletes(self, *, keep_gone: bool = False) -> list[str]:
        """Changed ones first: they must go before they can go in again."""
        return [*self.changed, *(() if keep_gone else self.gone)]

    def counts(self) -> dict[str, int]:
        return {
            "new": len(self.new),
            "gone": len(self.gone),
            "changed": len(self.changed),
            "unchanged": len(self.unchanged),
        }

    def line(self) -> str:
        """What the preview prints last, and the console shows as its summary."""
        c = self.counts()
        return f"{c['new']:,} new · {c['gone']:,} gone · {c['changed']:,} changed · {c['unchanged']:,} unchanged"


def plan_append(contents: StoreContents, passages: Sequence[Passage]) -> AppendPlan:
    """Sort the new snapshot's passages against the store. Identical texts go in once, as in a
    build, but under the id the store already has when it has one: a new id for a text already
    there would be refused as a duplicate on every try."""
    stored = contents.processed
    chosen: dict[str, Passage] = {}
    for passage in passages:
        pid = passage_id(passage)
        held = chosen.get(passage.text)
        if held is None or (stored.get(passage_id(held)) != passage.text and stored.get(pid) == passage.text):
            chosen[passage.text] = passage
    distinct = [p for p in passages if chosen.get(p.text) is p]
    ids = {passage_id(p) for p in distinct}
    unchanged, new, changed = [], [], []
    for passage in distinct:
        pid = passage_id(passage)
        if pid not in stored:
            new.append(pid)
        elif stored[pid] == passage.text:
            unchanged.append(pid)
        else:
            changed.append(pid)
    gone = sorted((set(stored) | contents.others) - ids)
    return AppendPlan(
        tuple(distinct), tuple(unchanged), tuple(new), tuple(changed), tuple(gone), len(passages) - len(distinct)
    )


def check_base(
    workdir: Path, from_ref: str, to_ref: str, embedder: EmbeddingClient, contents: StoreContents
) -> tuple[dict[str, Any], list[str]]:
    """The store's marker if it can take this append, and notes to print; ``AppendRefused`` if not.
    A store part-way through this same append is resumed; one heading elsewhere is refused."""
    if from_ref == to_ref:
        raise AppendRefused("the new snapshot is the one the store already holds")
    if not workdir.is_dir():
        raise AppendRefused(f"no LightRAG store at {workdir}")
    marker = read_marker(workdir)
    if marker is None:
        raise AppendRefused(f"{workdir} records no snapshot: run lightrag-build {from_ref} on it first")
    held, now = marker.get("procedures_ref"), store_state(marker)
    if now == APPENDING and (held != from_ref or marker.get("appending_to") != to_ref):
        raise AppendRefused(
            f"{workdir} is part-way through an append from {held} to {marker.get('appending_to')}: finish that one"
        )
    if held != from_ref:
        raise AppendRefused(f"{workdir} holds {held}, not {from_ref}")
    if marker.get("program_version", PROGRAM_VERSION) != PROGRAM_VERSION:
        raise AppendRefused(
            f"the store was built by {marker.get('program_version')}, this code is {PROGRAM_VERSION}: build again"
        )
    notes = []
    recorded = marker.get("embedding") or {}
    if contents.dimensions is not None and contents.dimensions != embedder.dimensions:
        raise AppendRefused(
            f"the store's vectors have {contents.dimensions} dimensions, the embedding model now gives "
            f"{embedder.dimensions}: build again, an append can't change models"
        )
    if recorded.get("model") and recorded["model"] != getattr(embedder, "model_id", None):
        raise AppendRefused(
            f"the store was embedded with {recorded['model']}, the embedding model now is "
            f"{embedder.model_id}: build again, an append can't change models"
        )
    if not recorded.get("model"):
        notes.append(
            f"the store doesn't record its embedding model (built before it did): appending with "
            f"{getattr(embedder, 'model_id', '?')}, {embedder.dimensions} dimensions. Check that's the one it "
            "was built with; the append records it"
        )
    appends = sum(1 for step in marker.get("lineage") or () if step.get("made") == "append")
    if appends + 1 >= REBUILD_AFTER:
        notes.append(
            f"this will be append {appends + 1} to the store: merged descriptions only grow, so consider "
            "building the snapshot's store again"
        )
    return marker, notes


def free_space_needed(workdir: Path, *, backup: bool) -> int:
    """A save rewrites the store's files; a backup is a whole copy more."""
    from .stores import size

    held = size(workdir)
    return held * (2 if backup else 1)


def backup_store(workdir: Path, to: Path) -> int:
    """A whole copy, checked file by file against the original's sizes. Returns bytes copied."""
    if to.exists():
        raise AppendRefused(f"{to} already exists")
    shutil.copytree(workdir, to)
    copied = 0
    for source in workdir.rglob("*"):
        if source.is_file():
            target = to / source.relative_to(workdir)
            if not target.is_file() or target.stat().st_size != source.stat().st_size:
                raise AppendRefused(f"the copy of {source.name} doesn't match the original")
            copied += target.stat().st_size
    return copied


def mark_appending(workdir: Path, marker: dict[str, Any], to_ref: str, plan: AppendPlan, keep_gone: bool) -> None:
    """Before the first change: from here, nothing searches the store until the append finishes.
    The planned counts are kept from the first start, so a resumed append reports the whole of it."""
    if store_state(marker) == APPENDING:
        return
    write_marker(
        workdir,
        {
            **marker,
            "state": APPENDING,
            "appending_to": to_ref,
            "append_started": _now(),
            "append_planned": {**plan.counts(), "keep_gone": keep_gone},
        },
    )


@dataclass
class LightRagAppend:
    deleted: int = 0
    delete_failed: dict[str, str] = field(default_factory=dict)  # passage id -> LightRAG's status word
    build: LightRagBuild | None = None  # the inserts, counted over the whole new snapshot
    asked_to_stop: bool = False
    stuck: bool = False

    @property
    def complete(self) -> bool:
        """Every delete done and the insert passes run through. Passages still failing after them
        don't hold the append open, as they don't hold a build open: ``lightrag-build`` retries them."""
        return (
            self.build is not None
            and not self.delete_failed
            and not self.asked_to_stop
            and not self.stuck
            and not self.build.stopped
            and not self.build.asked_to_stop
        )


async def append_lightrag(
    workdir: Path,
    plan: AppendPlan,
    embedder: EmbeddingClient,
    client: ModelClient,
    *,
    keep_gone: bool = False,
    concurrency: int = 16,
    save_every: int = 64,
    stop: threading.Event | None = None,
) -> LightRagAppend:
    """Deletes, then inserts, saving every ``save_every`` deletes or passages (``BatchedSaves``).
    ``stop`` ends it after saving what's in progress. The marker is the caller's: see ``finish``."""
    started = time.monotonic()
    llm = GatewayLlm(client)
    outcome = LightRagAppend()
    deletes = plan.deletes(keep_gone=keep_gone)
    async with open_rag(workdir, embedder, llm, concurrency=concurrency) as rag:
        saves = BatchedSaves(rag, save_every)
        try:
            for number, doc_id in enumerate(deletes, start=1):
                saves.hold()
                result = await rag.adelete_by_doc_id(doc_id)
                if result.status in DELETED:
                    outcome.deleted += 1
                else:
                    outcome.delete_failed[doc_id] = str(result.status)  # a fixed word: success, fail, …
                asked = stop is not None and stop.is_set()
                if saves.inserted(1) or number == len(deletes) or asked:
                    await saves.save()
                    logger.info(
                        "lightrag: [%d/%d] deleted; %d could not be; %d model calls",
                        outcome.deleted,
                        len(deletes),
                        len(outcome.delete_failed),
                        llm.calls,
                    )
                if asked:
                    outcome.asked_to_stop = True
                    logger.warning("lightrag: stopping as asked, with the deletes so far saved. Run again to resume")
                    return outcome
            if outcome.delete_failed:
                logger.warning(
                    "lightrag: %d passage(s) could not be deleted (%s); the ones changed among them aren't inserted "
                    "again this time. Run again to retry",
                    len(outcome.delete_failed),
                    ", ".join(sorted(set(outcome.delete_failed.values()))),
                )
            failed = set(outcome.delete_failed)
            inserting = [p for p in plan.passages if passage_id(p) not in failed]
            ended, state = await _insert_passes(rag, inserting, llm, saves, stop)
        finally:
            await saves.close()
    outcome.build = _built(inserting, len(inserting) + plan.duplicates, state, llm, ended, started)
    outcome.asked_to_stop = ended == "asked"
    outcome.stuck = ended == "stuck"
    return outcome


def finish(
    workdir: Path, marker: dict[str, Any], to_ref: str, outcome: LightRagAppend, embedder: EmbeddingClient
) -> dict[str, Any]:
    """After a complete append: the store holds ``to_ref``, with a lineage entry for the append."""
    current = read_marker(workdir) or marker
    planned = dict(current.get("append_planned") or {})
    build = outcome.build
    assert build is not None
    lineage = [dict(step) for step in current.get("lineage") or ()]
    if not lineage and current.get("procedures_ref"):
        lineage.append({"made": "build", "procedures_ref": current["procedures_ref"]})  # before lineage was kept
    lineage.append(
        {
            "made": "append",
            "from": current.get("procedures_ref"),
            "procedures_ref": to_ref,
            "started": current.get("append_started"),
            "at": _now(),
            **planned,
            "failed": build.failed,
        }
    )
    done = {
        key: value for key, value in current.items() if key not in ("appending_to", "append_started", "append_planned")
    }
    done.update(
        procedures_ref=to_ref,
        program_version=PROGRAM_VERSION,
        state=COMPLETE,
        embedding=embedding_of(embedder),
        passages=build.passages,
        processed=build.processed,
        failed=build.failed,
        lineage=lineage,
    )
    write_marker(workdir, done)
    return done
