"""LightRAG behind ``procedure.search`` (LDR-0019).

Knowledge graph and vectors, in ``mix`` mode. It indexes the redacted passages of a snapshot's
current procedures, one LightRAG document per passage, with the passage id as its file path. That
id comes back on every chunk, which gives the section and pages LightRAG doesn't keep itself.

Every model call goes through our gateway clients. The store is a local working directory: a
rebuild is the only update, and a generation artifact comes later.
``lightrag-hku`` is an optional dependency, imported only here.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from cfr_v2.contracts import (
    ProcedureHit,
    ProcedureSearchRequest,
    ProcedureSearchResult,
)
from cfr_v2.llm import EmbeddingClient, ModelBlockedError, ModelClient, ModelRequest, UnredactedPayloadError
from cfr_v2.llm.client import ChatTurn
from cfr_v2.llm.embeddings import EmbeddingSettings, embed_allowing_blocks
from cfr_v2.telemetry import get_logger, record_passages, record_stage, set_attributes, span

from .search import Passage
from .stores import APPENDING, COMPLETE, STORE_MARKER, read_marker, write_marker
from .stores import state as store_state

logger = get_logger(__name__)

PROGRAM_ID = "procedures.lightrag"
PROGRAM_VERSION = "lightrag-1"
BUILD_PURPOSE = "procedures.lightrag.index"
QUERY_PURPOSE = "procedures.lightrag.query"
CHUNK_TOKENS = 2048  # above the largest passage, so a passage stays one chunk
EMBEDDING_TOKEN_LIMIT = 8192
LLM_TIMEOUT_SEC = 1200  # as the gateway client's deadline: it does the retrying
BUILD_EMBEDDING_CONCURRENCY = 3  # the gateway answered 429 hundreds of times at 5; embeddings were never the slow part
BUILD_EMBEDDING_DEADLINE_SEC = 1800.0  # a 429 storm outlasted 600 s and halted a batch; nobody waits on a build
EMBEDDING_TIMEOUT_SEC = int(
    BUILD_EMBEDDING_DEADLINE_SEC
)  # as the client's deadline. LightRAG's default kills a call at
# 60 s and halts the pipeline
EMBEDDING_BATCH = 64  # as the embedding client's. LightRAG's 10 means several calls for one flush
INSERT_BATCH = 32  # two rounds at the default concurrency; a batch ends with its slowest passage
BUILD_PASSES = 3  # failed passages get this many tries in one build; their extraction is cached
STUCK_BATCHES = 3  # this many batches in a row adding nothing means something is down: stop spending
# A save rewrites every store in full: late in a large build, about 1.3 GB a time. Saving after each
# batch's worth instead of after each passage cuts that by the batch's size (LDR-0030).
SAVE_EVERY = 64  # passages between saves; a crash or a Force stop redoes at most these. 1: after each
SAVE_TRIES = 3  # a save makes the vector stores' embedding calls, so it can fail like any call
SAVE_BACKOFF_SEC = 30.0  # first wait between tries; doubles
VECTOR_STORES = ("entities_vdb", "relationships_vdb", "chunks_vdb")
CHUNKS = 50  # chunks LightRAG returns per search, before the per-document cap and the limit
DEFAULT_INSTRUCTIONS = "Follow the instructions in the message."
# LightRAG errors known to carry only ids, counts and storage names. Others can quote a model's
# answer or an entity, so they keep their place in the code and lose their message.
SAFE_LIGHTRAG_ERRORS = (
    "Aborting pipeline batch",
    "Error embedding pending vector ops",
    "Failed to extract document",
    "Merging stage failed",
    "Pipeline halted",
)


class StoreMismatchError(RuntimeError):
    """A LightRAG store used with, or built from, a snapshot other than its own."""


def read_store_marker(workdir: Path) -> dict[str, Any] | None:
    return read_marker(workdir)


def check_store(workdir: Path, procedures_ref: str) -> dict[str, Any]:
    """The store's marker, if it holds ``procedures_ref`` whole; otherwise ``StoreMismatchError``.
    A store without a marker predates it: rebuild it from the snapshot (passages already in the
    graph are skipped), which writes one. A store part-way through an append (LDR-0043) holds
    neither snapshot whole, so nothing searches it until the append finishes."""
    if not workdir.is_dir():
        raise StoreMismatchError(f"no LightRAG store at {workdir}: run lightrag-build {procedures_ref} to build it")
    marker = read_marker(workdir)
    if marker is None:
        raise StoreMismatchError(
            f"{workdir} has no {STORE_MARKER}: run lightrag-build {procedures_ref} on it to record its snapshot"
        )
    if store_state(marker) == APPENDING:
        raise StoreMismatchError(
            f"{workdir} is part-way through an append from {marker.get('procedures_ref')} to "
            f"{marker.get('appending_to')}: run lightrag-append again to finish it"
        )
    if marker.get("procedures_ref") != procedures_ref:
        raise StoreMismatchError(f"{workdir} was built from {marker.get('procedures_ref')}, not {procedures_ref}")
    return marker


def embedding_of(embedder: EmbeddingClient) -> dict[str, Any]:
    return {"model": getattr(embedder, "model_id", None), "dimensions": embedder.dimensions}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_store_marker(workdir: Path, procedures_ref: str, build: LightRagBuild, embedder: EmbeddingClient) -> None:
    """After a build: the snapshot, the counts, the embedding model, and a lineage entry (one per
    snapshot built: a build resumed again only updates its own)."""
    previous = read_marker(workdir) or {}
    lineage = [dict(step) for step in previous.get("lineage") or ()]
    if not (lineage and lineage[-1].get("made") == "build" and lineage[-1].get("procedures_ref") == procedures_ref):
        lineage.append({"made": "build", "procedures_ref": procedures_ref, "at": _now()})
    lineage[-1].update(passages=build.passages, processed=build.processed, failed=build.failed)
    write_marker(
        workdir,
        {
            "procedures_ref": procedures_ref,
            "program_version": PROGRAM_VERSION,
            "state": COMPLETE,
            "embedding": embedding_of(embedder),
            "passages": build.passages,
            "processed": build.processed,
            "failed": build.failed,
            "lineage": lineage,
        },
    )


class NoTextInLightRagLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not record.getMessage().lstrip("[] ").startswith(SAFE_LIGHTRAG_ERRORS):
            record.msg, record.args = "message withheld (%s:%d)", (record.filename, record.lineno)
        return True


def build_embedding_settings(settings: EmbeddingSettings) -> EmbeddingSettings:
    """Embeddings for a build: fewer at once, and a deadline long enough to sit out rate limiting.
    A lower concurrency or longer deadline set in the environment is kept."""
    return replace(
        settings,
        max_concurrency=min(settings.max_concurrency, BUILD_EMBEDDING_CONCURRENCY),
        call_deadline_sec=max(settings.call_deadline_sec, BUILD_EMBEDDING_DEADLINE_SEC),
    )


def passage_id(passage: Passage) -> str:
    return f"p-{passage.document.source_sha256[:16]}-{passage.section.index:04d}"


# One search's own count of gateway blocks (a one-item list, so the calls LightRAG runs in copies of
# the search's context add to it). The counters on ``GatewayLlm`` are the whole process's: searches
# run at once, so a change in them can't say whose call was blocked.
_SEARCH_BLOCKS: ContextVar[list[int] | None] = ContextVar("lightrag_search_blocks", default=None)


def _count_search_block() -> None:
    if (blocks := _SEARCH_BLOCKS.get()) is not None:
        blocks[0] += 1


class GatewayLlm:
    """LightRAG's model function over our client. A blocked call gives an empty answer, so one
    firewall block costs that chunk's graph entries, not the build."""

    def __init__(self, client: ModelClient) -> None:
        self.client = client
        self.calls = 0
        self.blocked = 0
        self.blocked_embeddings = 0
        self.reset_timings()

    def reset_timings(self) -> None:
        self.slowest_call = self.slowest_embedding = 0.0
        self.embedding_calls = 0

    async def __call__(
        self, prompt: str, system_prompt: str | None = None, history_messages: Sequence[dict] | None = None, **_: Any
    ) -> str:
        turns = [
            ChatTurn(role=m["role"], content=m["content"])
            for m in history_messages or ()
            if m.get("role") in ("user", "assistant")
        ]
        request = ModelRequest(
            program_id=PROGRAM_ID,
            program_version=PROGRAM_VERSION,
            case_id="procedure-corpus",
            task_id="lightrag",
            instructions=system_prompt or DEFAULT_INSTRUCTIONS,
            inputs={},
            redacted=True,
            messages=(*turns, ChatTurn(role="user", content=prompt)),
        )
        self.calls += 1
        started, outcome = time.monotonic(), "failed"
        try:
            response = await self.client.complete(request)
            outcome = "ok"
        except ModelBlockedError:
            outcome = "blocked"
            self.blocked += 1
            _count_search_block()
            logger.warning(
                "lightrag: the gateway blocked a model call (%d so far); it gives an empty answer", self.blocked
            )
            return ""
        finally:
            took = time.monotonic() - started
            self.slowest_call = max(self.slowest_call, took)
            record_stage("model_call", took, outcome)
        return str(response.output["text"])


def _embedding_func(embedder: EmbeddingClient, llm: GatewayLlm):
    """LightRAG can't tell which record failed a flush and keeps them all for the next one, so a
    single blocked text would halt every later flush. A blocked text stored while indexing gets a
    vector that matches nothing, and stays reachable through the graph. Not a zero vector: the
    store normalises it to NaN, which ranks first in every search."""
    from lightrag.utils import EmbeddingFunc

    unmatched = np.zeros(embedder.dimensions, dtype=np.float32)
    unmatched[0] = 1.0

    async def embed(texts: list[str], context: str | None = None, **_: Any) -> np.ndarray:
        if context == "query":
            return np.asarray(await embedder.embed(texts, purpose=QUERY_PURPOSE, redacted=True), dtype=np.float32)
        started = time.monotonic()
        found = await embed_allowing_blocks(embedder, texts, purpose=BUILD_PURPOSE, redacted=True)
        took = time.monotonic() - started
        llm.embedding_calls += 1
        llm.slowest_embedding = max(llm.slowest_embedding, took)
        record_stage("embedding", took)
        blocked = sum(v is None for v in found)
        if blocked:
            llm.blocked_embeddings += blocked
            logger.warning(
                "lightrag: the gateway blocked %d of %d texts to embed (%d so far); vector search won't find them",
                blocked,
                len(texts),
                llm.blocked_embeddings,
            )
        return np.asarray([unmatched if v is None else v for v in found], dtype=np.float32)

    # "Asymmetric" is what makes LightRAG pass the context through, or a query looks like indexing.
    return EmbeddingFunc(
        embedding_dim=embedder.dimensions,
        max_token_size=EMBEDDING_TOKEN_LIMIT,
        func=embed,
        supports_asymmetric=True,
    )


def _trace_flushes(rag: Any) -> None:
    """A span around LightRAG's save after each passage: its embeddings, the storage lock they are
    made under, and the file writes. The method is private, so a version without it only loses
    this span, and it never changes what the flush does."""
    flush = getattr(rag, "_insert_done", None)
    if flush is None:
        logger.warning("lightrag: no _insert_done in this version, so flushes won't be traced")
        return

    async def traced(*args: Any, **kwargs: Any) -> None:
        started, outcome = time.monotonic(), "failed"
        with span("procedures.lightrag.flush") as flush_span:
            try:
                await flush(*args, **kwargs)
                outcome = "ok"
            finally:
                set_attributes(flush_span, outcome=outcome)
                record_stage("flush", time.monotonic() - started, outcome)

    rag._insert_done = traced


class SaveError(RuntimeError):
    """A held batch couldn't be saved. Nothing of it was written: the store is as at its last save."""


class BatchedSaves:
    """Save the store once per batch's worth of passages instead of once per passage (LDR-0030).

    LightRAG 1.5 saves every store in full after each passage (``_insert_done``), and the doc-status
    store saves itself on each status change. With JSON stores, each save rewrites files that reach
    a gigabyte; a scanner that holds every deleted copy open fills the disk.

    While holding, every store's ``index_done_callback`` is a no-op, whoever calls it: the commits,
    LightRAG's own write-ahead saves, and the status store. ``save`` then writes the vector stores
    first (they make the embedding calls, so they can fail; tried ``SAVE_TRIES`` times), and the other
    stores, the document statuses among them, only once they succeed. So the disk only ever holds
    whole save points: a passage on disk as PROCESSED always has its vectors, and a passage whose
    batch wasn't saved is simply not there, and is inserted again on resume.

    If LightRAG discards its pending buffers while holding (an internal abort), what was held may
    no longer be whole: the batch is ``tainted`` and never saved.
    """

    def __init__(self, rag: Any, every: int = SAVE_EVERY) -> None:
        self.rag, self.every = rag, max(1, every)
        self.holding = False
        self.pending = 0  # passages inserted since the last save
        self.tainted = False
        self.failed = False
        self._stores: list[tuple[str, Any, Any]] = []  # (attribute, store, its own callback)
        if self.every == 1:
            return  # LightRAG's own saving, unchanged
        named = {id(getattr(rag, name, None)): name for name in vars(rag) if not name.startswith("_")}
        for store in rag._index_storages():
            real = store.index_done_callback
            store.index_done_callback = self._held(real)
            self._stores.append((named.get(id(store), type(store).__name__), store, real))
        commit, discard = rag._insert_done, rag._discard_pending_index_ops

        async def insert_done(*args: Any, **kwargs: Any) -> None:
            if not self.holding:  # outside a batch (clearing failed passages, draining): as before
                await commit(*args, **kwargs)

        async def discard_pending(*args: Any, **kwargs: Any) -> None:
            if self.holding:
                self.tainted = True
            await discard(*args, **kwargs)

        rag._insert_done, rag._discard_pending_index_ops = insert_done, discard_pending

    @property
    def batched(self) -> bool:
        return bool(self._stores)

    def _held(self, real: Any) -> Any:
        async def callback(*args: Any, **kwargs: Any) -> Any:
            if self.holding:
                return None
            return await real(*args, **kwargs)

        return callback

    def hold(self) -> None:
        if self.batched:
            self.holding = True

    def inserted(self, passages: int) -> bool:
        """Count passages inserted while holding; True when a save is due."""
        self.pending += passages
        return self.pending >= self.every

    async def save(self) -> None:
        """Write what's held: vector stores first, the rest only if they succeed."""
        if not self.batched or not self.holding:
            self.pending = 0
            return
        if self.tainted:
            raise SaveError("LightRAG discarded pending writes during the batch; it was not saved")
        started, outcome = time.monotonic(), "failed"
        vectors = [real for name, _, real in self._stores if name in VECTOR_STORES]
        others = [real for name, _, real in self._stores if name not in VECTOR_STORES]
        with span("procedures.lightrag.flush", passages=self.pending) as save_span:
            try:
                wait = SAVE_BACKOFF_SEC
                for attempt in range(1, SAVE_TRIES + 1):
                    errors = [e for e in await asyncio.gather(*(f() for f in vectors), return_exceptions=True) if e]
                    if not [e for e in errors if isinstance(e, BaseException)]:
                        break
                    first = next(e for e in errors if isinstance(e, BaseException))
                    if isinstance(first, asyncio.CancelledError):
                        raise first
                    if attempt == SAVE_TRIES:
                        self.failed = True
                        raise SaveError(
                            f"the vector stores couldn't be saved after {SAVE_TRIES} tries "
                            f"({type(first).__name__}); nothing since the last save was written"
                        ) from first
                    logger.warning(
                        "lightrag: saving the vectors failed (%s), try %d of %d; again in %.0fs",
                        type(first).__name__,
                        attempt,
                        SAVE_TRIES,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    wait *= 2
                errors = [e for e in await asyncio.gather(*(f() for f in others), return_exceptions=True) if e]
                failures = [e for e in errors if isinstance(e, BaseException)]
                if failures:
                    self.failed = True
                    raise SaveError(f"a store couldn't be saved ({type(failures[0]).__name__})") from failures[0]
                outcome = "ok"
            finally:
                set_attributes(save_span, outcome=outcome)
                record_stage("flush", time.monotonic() - started, outcome)
        logger.info(
            "lightrag: saved the store: %d passages since the last save, in %.0fs",
            self.pending,
            time.monotonic() - started,
        )
        self.pending, self.holding = 0, False

    async def close(self) -> None:
        """Save what's pending, unless it can't be trusted; then hand saving back to LightRAG."""
        try:
            if self.holding and not (self.tainted or self.failed):
                try:
                    await self.save()
                except Exception as exc:  # noqa: BLE001 - the error that ended the build matters more
                    logger.error("lightrag: the last batch wasn't saved: %s", exc)
            elif self.holding:
                logger.warning(
                    "lightrag: the last batch wasn't saved (%s): the next run redoes it",
                    "pending writes were discarded" if self.tainted else "its save failed",
                )
        finally:
            self.holding = False
            for _, store, real in self._stores:
                store.index_done_callback = real


def _import_lightrag() -> Any:
    """LightRAG's class, imported without its side effect on logging. nano-vectordb, which LightRAG
    loads, calls ``logging.basicConfig(level=INFO)`` on import: on a root logger without handlers that
    adds one, and every record after it is printed a second time, outside our formatter and masker."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        import nano_vectordb  # noqa: F401 - imported here so its basicConfig runs inside this guard
        from lightrag import LightRAG
    finally:
        for handler in [h for h in root.handlers if h not in handlers]:
            root.removeHandler(handler)
        root.setLevel(level)
    return LightRAG


@asynccontextmanager
async def open_rag(
    workdir: Path, embedder: EmbeddingClient, llm: GatewayLlm, *, concurrency: int = 16, query_only: bool = False
) -> AsyncIterator[Any]:
    """``query_only``: for searching. LightRAG's query cache is then off: it would cache each query's
    keywords and, after every query, save the whole LLM cache file, which holds every extraction
    answer of the build, for queries that almost never repeat. The extraction cache, which builds,
    appends and deletes use, has its own switch and stays on."""
    LightRAG = _import_lightrag()

    # A plain function on purpose: LightRAG deep-copies its configuration on every extraction, which
    # would reach the gateway client's locks through an object. Functions are copied by reference.
    async def model_func(*args: Any, **kwargs: Any) -> str:
        return await llm(*args, **kwargs)

    workdir.mkdir(parents=True, exist_ok=True)
    rag = LightRAG(
        working_dir=str(workdir),
        llm_model_func=model_func,
        embedding_func=_embedding_func(embedder, llm),
        embedding_batch_num=EMBEDDING_BATCH,
        chunk_token_size=CHUNK_TOKENS,
        chunk_overlap_token_size=0,
        llm_model_max_async=concurrency,
        max_parallel_insert=concurrency,  # a passage is one chunk, so this is what limits extraction
        default_llm_timeout=LLM_TIMEOUT_SEC,
        default_embedding_timeout=EMBEDDING_TIMEOUT_SEC,
        enable_llm_cache=not query_only,
    )
    for name in ("lightrag", "nano-vectordb"):  # at INFO they print entity names and queries
        logging.getLogger(name).setLevel(logging.ERROR)
    for name in ("httpx", "httpx2"):  # a line per request would bury everything else
        logging.getLogger(name).setLevel(logging.WARNING)
    lightrag_logs = logging.getLogger("lightrag")
    if not any(isinstance(f, NoTextInLightRagLogs) for f in lightrag_logs.filters):
        lightrag_logs.addFilter(NoTextInLightRagLogs())
    await rag.initialize_storages()
    _trace_flushes(rag)
    try:
        yield rag
    finally:
        try:
            await rag.finalize_storages()
        finally:
            await _stop_workers(rag)


async def _stop_workers(rag: Any) -> None:
    """LightRAG runs each model role (extract, keyword, query) and the embeddings through its own
    queue workers, and closing the storages leaves them running. Left to the end of the event loop,
    they fail one by one with "Task was destroyed but it is pending" and "no running event loop"."""
    roles = getattr(rag, "role_llm_funcs", None) or {}
    funcs = [*roles.values(), rag.llm_model_func, getattr(rag.embedding_func, "func", None)]
    seen: set[int] = set()
    for func in funcs:
        shutdown = getattr(func, "shutdown", None)
        if shutdown is None or id(func) in seen:
            continue
        seen.add(id(func))
        try:
            await shutdown()
        except Exception as exc:  # noqa: BLE001 - the build is saved by now; a worker that won't stop is only noise
            # LightRAG's shutdown errors name its queues, not documents: the message says which
            logger.warning("lightrag: could not stop LightRAG's call workers: %s: %.200s", type(exc).__name__, exc)


@dataclass
class LightRagBuild:
    passages: int
    processed: int
    failed: int
    model_calls: int
    blocked_calls: int
    blocked_embeddings: int = 0
    duplicates: int = 0
    stopped: bool = False  # batches in a row added nothing
    asked_to_stop: bool = False  # stopped as asked, after saving the batch in progress


@dataclass
class _Progress:
    processed: int
    failed: dict[str, str]  # passage id -> reason
    queued: int


def _reason(error: str | None) -> str:
    """A fixed word, as LightRAG's stored message can quote text."""
    message = (error or "").lower()
    if "cancelled by internal error" in message:
        return "halted"
    if "block" in message:
        return "blocked"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    return "other"


def _reasons(failed: Mapping[str, str]) -> str:
    return ", ".join(f"{reason} {count}" for reason, count in Counter(failed.values()).most_common())


def _sample(ids: Iterable[str], shown: int = 10) -> str:
    ordered = sorted(ids)
    more = f" and {len(ordered) - shown} more" if len(ordered) > shown else ""
    return ", ".join(ordered[:shown]) + more


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"


async def _progress(rag: Any, ids: set[str]) -> _Progress:
    """Ours only: LightRAG also keeps a FAILED row for each duplicate insert, under its own id."""
    from lightrag.base import DocStatus

    rows = await rag.doc_status.get_docs_by_statuses(list(DocStatus))
    processed = queued = 0
    reasons: dict[str, str] = {}
    for doc_id, row in rows.items():
        if doc_id not in ids:
            continue
        if row.status == DocStatus.PROCESSED:
            processed += 1
        elif row.status == DocStatus.FAILED:
            reasons[doc_id] = _reason(row.error_msg)
        else:
            queued += 1
    return _Progress(processed, reasons, queued)


async def _clear_failed(rag: Any) -> int:
    """LightRAG never retries a failed document by itself, and a second insert is a duplicate."""
    from lightrag.base import DocStatus

    cleared = 0
    for doc_id in await rag.doc_status.get_docs_by_statuses([DocStatus.FAILED]):
        result = await rag.adelete_by_doc_id(doc_id)
        if result.status == "success":
            cleared += 1
        else:
            logger.warning("lightrag: could not clear failed passage %s: %s", doc_id, result.status)
    return cleared


async def _insert_pass(
    rag: Any,
    passages: Sequence[Passage],
    llm: GatewayLlm,
    number: int,
    saves: BatchedSaves,
    stop: threading.Event | None = None,
) -> str | None:
    """Insert what isn't in the graph yet. None when the pass ran through; else why it stopped early
    ("stuck", or "asked" when ``stop`` was set: after saving the batch in progress).

    ``ainsert`` returns normally even when the pipeline halts, so only the stored statuses say what
    happened. Passages a halt left queued are processed first, and never inserted again.
    """
    from lightrag.base import DocStatus

    ids = {passage_id(p) for p in passages}
    with span("procedures.lightrag.recover", new_trace=True, pass_no=number):
        started = time.monotonic()
        cleared = await _clear_failed(rag)  # LightRAG rebuilds what they touched in the graph: model calls
        clearing = time.monotonic() - started
        if cleared:
            record_stage("clear", clearing)
        started = time.monotonic()
        await rag.apipeline_process_enqueue_documents()  # passages a halt left queued
        draining = time.monotonic() - started
        record_stage("drain", draining)
    known = await rag.doc_status.get_docs_by_statuses(list(DocStatus))
    waiting = [p for p in passages if passage_id(p) not in known]
    state = await _progress(rag, ids)
    logger.info(
        "lightrag: pass %d of %d: cleared %d failed in %.0fs, finished queued passages in %.0fs; "
        "%d in the graph, %d to insert, %d failed, %d queued",
        number,
        BUILD_PASSES,
        cleared,
        clearing,
        draining,
        state.processed,
        len(waiting),
        len(state.failed),
        state.queued,
    )
    stuck = 0
    batches = -(-len(waiting) // INSERT_BATCH)
    for batch_no, start in enumerate(range(0, len(waiting), INSERT_BATCH), start=1):
        batch = waiting[start : start + INSERT_BATCH]
        batch_ids = [passage_id(p) for p in batch]
        batch_started, calls_before = time.monotonic(), llm.calls
        # A trace per batch: a whole build would be thousands of spans, unseen until it ended
        with span(
            "procedures.lightrag.batch", new_trace=True, pass_no=number, batch_no=batch_no, passages=len(batch)
        ) as batch_span:
            saves.hold()
            await rag.ainsert([p.text for p in batch], ids=batch_ids, file_paths=batch_ids)
            asked = stop is not None and stop.is_set()
            if saves.inserted(len(batch)) or batch_no == batches or asked:
                await saves.save()  # inside the batch's trace
            before, state = state, await _progress(rag, ids)
            set_attributes(
                batch_span,
                processed=state.processed,
                failed=len(state.failed),
                queued=state.queued,
                outcome="ok" if len(state.failed) <= len(before.failed) else "lost_passages",
            )
        took = time.monotonic() - batch_started
        record_stage("batch", took)
        record_passages(processed=state.processed, failed=len(state.failed), queued=state.queued)
        added = state.processed - before.processed
        left = _duration(took / added * (len(passages) - state.processed)) if added else "unknown"  # at this pace
        logger.info(
            "lightrag: [%d/%d] passages in the graph; %d failed, %d queued; batch %d of %d took %.0fs; "
            "%d new model calls, %d blocked; slowest model call %.0fs; %d embedding calls, slowest %.0fs; "
            "about %s left",
            state.processed,
            len(passages),
            len(state.failed),
            state.queued,
            batch_no,
            batches,
            took,
            llm.calls - calls_before,  # per batch: the masker hides some four-digit totals
            llm.blocked,
            llm.slowest_call,
            llm.embedding_calls,
            llm.slowest_embedding,
            left,
        )
        llm.reset_timings()
        lost = {k: v for k, v in state.failed.items() if k not in before.failed}
        if lost:
            logger.warning(
                "lightrag: batch %d lost %d passage(s) (%s), %d queued; the next pass retries them: %s",
                batch_no,
                len(lost),
                _reasons(lost),
                state.queued,
                _sample(lost),
            )
        if asked:
            logger.warning("lightrag: stopping as asked, with the batch in progress saved. Run this again to resume")
            return "asked"
        stuck = 0 if state.processed > before.processed else stuck + 1
        if stuck == STUCK_BATCHES:
            await saves.save()
            logger.error(
                "lightrag: stopping: %d batches in a row added nothing (%s). Check the gateway, then run "
                "this again to resume",
                stuck,
                _reasons(state.failed) or "no failures recorded",
            )
            return "stuck"
    return None


async def build_lightrag(
    workdir: Path,
    passages: Sequence[Passage],
    embedder: EmbeddingClient,
    client: ModelClient,
    *,
    concurrency: int = 16,
    procedures_ref: str | None = None,
    save_every: int = SAVE_EVERY,
    stop: threading.Event | None = None,
) -> LightRagBuild:
    """Passages already processed are skipped, so an interrupted build can resume. Failed ones are
    tried again, up to ``BUILD_PASSES`` times, and whatever still fails is reported.

    The store is saved every ``save_every`` passages, at the end of a batch (``BatchedSaves``); 1 saves
    after each passage, as LightRAG does by itself. ``stop``, set from another thread, ends the build
    after the batch in progress is saved.

    Identical passages go in once: LightRAG would fail the second as a duplicate on every try.
    With ``procedures_ref``, the store records the snapshot it was built from, and a store already
    built from another one is refused: LightRAG never removes a passage, so the two would mix.
    """
    if procedures_ref is not None:
        marker = read_store_marker(workdir)
        if marker is not None and store_state(marker) == APPENDING:
            raise StoreMismatchError(
                f"{workdir} is part-way through an append to {marker.get('appending_to')}: "
                "run lightrag-append again to finish it"
            )
        if marker is not None and marker.get("procedures_ref") != procedures_ref:
            raise StoreMismatchError(
                f"{workdir} was built from {marker.get('procedures_ref')}: build {procedures_ref} in a new directory"
            )
    distinct = distinct_passages(passages)
    ids = {passage_id(p) for p in distinct}
    skipped = {passage_id(p) for p in passages} - ids
    if skipped:
        logger.warning(
            "lightrag: %d passage(s) repeat another's text and go in once: %s", len(skipped), _sample(skipped)
        )
    logger.info(
        "lightrag: %d passages to index at concurrency %d, saving every %d",
        len(distinct),
        concurrency,
        save_every,
    )
    started = time.monotonic()
    llm = GatewayLlm(client)
    async with open_rag(workdir, embedder, llm, concurrency=concurrency) as rag:
        saves = BatchedSaves(rag, save_every)
        try:
            ended, state = await _insert_passes(rag, distinct, llm, saves, stop)
        finally:
            await saves.close()
    build = _built(distinct, len(passages), state, llm, ended, started)
    if procedures_ref is not None:
        _write_store_marker(workdir, procedures_ref, build, embedder)
    return build


def distinct_passages(passages: Sequence[Passage]) -> list[Passage]:
    """One passage per text, the first of each: LightRAG fails a second copy as a duplicate."""
    return list({p.text: p for p in reversed(passages)}.values())[::-1]


async def _insert_passes(
    rag: Any, distinct: Sequence[Passage], llm: GatewayLlm, saves: BatchedSaves, stop: threading.Event | None
) -> tuple[str | None, _Progress]:
    """Up to ``BUILD_PASSES`` passes, until every passage is in or a pass stops early."""
    ids = {passage_id(p) for p in distinct}
    ended: str | None = None
    for number in range(1, BUILD_PASSES + 1):
        ended = await _insert_pass(rag, distinct, llm, number, saves, stop)
        state = await _progress(rag, ids)
        if ended or state.processed == len(distinct):
            break
    return ended, state


def _built(
    distinct: Sequence[Passage], given: int, state: _Progress, llm: GatewayLlm, ended: str | None, started: float
) -> LightRagBuild:
    missing = len(distinct) - state.processed
    logger.log(
        logging.WARNING if missing else logging.INFO,
        "lightrag: build ended after %s: %d of %d in the graph; %d failed, %d queued; %d model calls; "
        "blocked: %d model calls, %d embeddings",
        _duration(time.monotonic() - started),
        state.processed,
        len(distinct),
        len(state.failed),
        state.queued,
        llm.calls,
        llm.blocked,
        llm.blocked_embeddings,
    )
    if state.failed:
        logger.warning("lightrag: still failing (%s): %s", _reasons(state.failed), _sample(state.failed))
    return LightRagBuild(
        len(distinct),
        state.processed,
        len(state.failed),
        llm.calls,
        llm.blocked,
        llm.blocked_embeddings,
        given - len(distinct),
        ended == "stuck",
        ended == "asked",
    )


class LightRagProcedureSearch:
    def __init__(self, rag: Any, procedures_ref: str, passages: Sequence[Passage], llm: GatewayLlm) -> None:
        self.rag = rag
        self.llm = llm
        self.procedures_ref = procedures_ref
        self.passages = {passage_id(p): p for p in passages}

    async def search(self, request: ProcedureSearchRequest) -> ProcedureSearchResult:
        from lightrag import QueryParam

        if not request.redacted:
            raise UnredactedPayloadError(f"{PROGRAM_ID}: payload not marked redacted")
        param = QueryParam(mode="mix", chunk_top_k=CHUNKS, enable_rerank=False)
        blocks = [0]  # this search's own: others run at the same time on the same GatewayLlm
        token = _SEARCH_BLOCKS.set(blocks)
        try:
            data = (await self.rag.aquery_data(request.query, param)).get("data") or {}
        except ModelBlockedError:  # one blocked claim isn't a failed run; anything else is raised
            logger.warning("lightrag: the gateway blocked a search, so it found nothing")
            return ProcedureSearchResult(procedures_ref=self.procedures_ref, blocked=True, hits=())
        finally:
            _SEARCH_BLOCKS.reset(token)
        seen: set[str] = set()
        per_document: Counter[str] = Counter()
        hits: list[ProcedureHit] = []
        for rank, chunk in enumerate(data.get("chunks", ()), start=1):
            key = chunk.get("file_path", "")
            passage = self.passages.get(key)
            if passage is None or key in seen:
                continue
            seen.add(key)
            document, section = passage.document, passage.section
            if per_document[document.source_sha256] >= request.max_per_document:
                continue
            per_document[document.source_sha256] += 1
            hits.append(
                ProcedureHit(
                    procedure_name=document.name,
                    file_name=document.file_name,
                    source_sha256=document.source_sha256,
                    annual_review_date=document.annual_review_date,
                    monthly_revision_date=document.monthly_revision_date,
                    section_index=section.index,
                    section_title=section.title,
                    part=section.part,
                    page_start=section.page_start,
                    page_end=section.page_end,
                    text=section.text,
                    rank=rank,
                )
            )
            if len(hits) == request.limit:
                break
        return ProcedureSearchResult(
            # A blocked keyword call raises nothing: LightRAG just finds less, or nothing
            procedures_ref=self.procedures_ref,
            blocked=blocks[0] > 0,
            hits=tuple(hits),
        )
