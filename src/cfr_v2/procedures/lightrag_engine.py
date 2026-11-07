"""LightRAG behind ``procedure.search``: the bake-off's challenger (LDR-0019).

Knowledge graph and vectors, in ``mix`` mode. It indexes the same redacted passages as the hybrid
baseline, one LightRAG document per passage, with the passage id as its file path. That id comes
back on every chunk, which gives the section and pages LightRAG doesn't keep itself.

Every model call goes through our gateway clients. The store is a local working directory: a
rebuild is the only update, and a generation artifact comes later if LightRAG wins.
``lightrag-hku`` is an optional dependency, imported only here.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from cfr_v2.contracts import (
    Classification,
    ProcedureHit,
    ProcedureIndex,
    ProcedureSearchRequest,
    ProcedureSearchResult,
)
from cfr_v2.llm import EmbeddingClient, ModelBlockedError, ModelClient, ModelRequest, UnredactedPayloadError
from cfr_v2.llm.client import ChatTurn
from cfr_v2.llm.embeddings import embed_allowing_blocks
from cfr_v2.telemetry import get_logger, record_passages, record_stage, set_attributes, span

from .search import _READER, CANDIDATES, Passage, load_passages

logger = get_logger(__name__)

PROGRAM_ID = "procedures.lightrag"
PROGRAM_VERSION = "lightrag-1"
BUILD_PURPOSE = "procedures.lightrag.index"
QUERY_PURPOSE = "procedures.lightrag.query"
CHUNK_TOKENS = 2048  # above the largest passage, so a passage stays one chunk
EMBEDDING_TOKEN_LIMIT = 8192
LLM_TIMEOUT_SEC = 1200  # as the gateway client's deadline: it does the retrying
EMBEDDING_TIMEOUT_SEC = 600  # likewise. LightRAG's default kills a call at 60 s and halts the pipeline
EMBEDDING_BATCH = 64  # as the embedding client's. LightRAG's 10 means several calls for one flush
INSERT_BATCH = 32  # two rounds at the default concurrency; a batch ends with its slowest passage
BUILD_PASSES = 3  # failed passages get this many tries in one build; their extraction is cached
STUCK_BATCHES = 3  # this many batches in a row adding nothing means something is down: stop spending
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


class NoTextInLightRagLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not record.getMessage().lstrip("[] ").startswith(SAFE_LIGHTRAG_ERRORS):
            record.msg, record.args = "message withheld (%s:%d)", (record.filename, record.lineno)
        return True


def passage_id(passage: Passage) -> str:
    return f"p-{passage.document.source_sha256[:16]}-{passage.section.index:04d}"


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


@asynccontextmanager
async def open_rag(
    workdir: Path, embedder: EmbeddingClient, llm: GatewayLlm, *, concurrency: int = 16
) -> AsyncIterator[Any]:
    from lightrag import LightRAG

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
        await rag.finalize_storages()
        for func in (rag.llm_model_func, rag.embedding_func.func):  # its queue workers, before the loop closes
            shutdown = getattr(func, "shutdown", None)
            if shutdown:
                await shutdown()


@dataclass
class LightRagBuild:
    passages: int
    processed: int
    failed: int
    model_calls: int
    blocked_calls: int
    blocked_embeddings: int = 0
    duplicates: int = 0
    stopped: bool = False


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


async def _insert_pass(rag: Any, passages: Sequence[Passage], llm: GatewayLlm, number: int) -> bool:
    """Insert what isn't in the graph yet. False if it stopped early.

    ``ainsert`` returns normally even when the pipeline halts, so only the stored statuses say what
    happened. Passages a halt left queued are processed first, and never inserted again.
    """
    from lightrag.base import DocStatus

    ids = {passage_id(p) for p in passages}
    with span("procedures.lightrag.recover", new_trace=True, pass_no=number):
        cleared = await _clear_failed(rag)
        await rag.apipeline_process_enqueue_documents()
    known = await rag.doc_status.get_docs_by_statuses(list(DocStatus))
    waiting = [p for p in passages if passage_id(p) not in known]
    state = await _progress(rag, ids)
    logger.info(
        "lightrag: pass %d of %d: cleared %d failed; %d in the graph, %d to insert, %d failed, %d queued",
        number,
        BUILD_PASSES,
        cleared,
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
        batch_started = time.monotonic()
        # A trace per batch: a whole build would be thousands of spans, unseen until it ended
        with span(
            "procedures.lightrag.batch", new_trace=True, pass_no=number, batch_no=batch_no, passages=len(batch)
        ) as batch_span:
            await rag.ainsert([p.text for p in batch], ids=batch_ids, file_paths=batch_ids)
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
            "%d model calls, %d blocked; slowest model call %.0fs; %d embedding calls, slowest %.0fs; "
            "about %s left",
            state.processed,
            len(passages),
            len(state.failed),
            state.queued,
            batch_no,
            batches,
            took,
            llm.calls,
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
        stuck = 0 if state.processed > before.processed else stuck + 1
        if stuck == STUCK_BATCHES:
            logger.error(
                "lightrag: stopping: %d batches in a row added nothing (%s). Check the gateway, then run "
                "this again to resume",
                stuck,
                _reasons(state.failed) or "no failures recorded",
            )
            return False
    return True


async def build_lightrag(
    workdir: Path,
    passages: Sequence[Passage],
    embedder: EmbeddingClient,
    client: ModelClient,
    *,
    concurrency: int = 16,
) -> LightRagBuild:
    """Passages already processed are skipped, so an interrupted build can resume. Failed ones are
    tried again, up to ``BUILD_PASSES`` times, and whatever still fails is reported.

    Identical passages go in once: LightRAG would fail the second as a duplicate on every try.
    """
    distinct = list({p.text: p for p in reversed(passages)}.values())[::-1]
    ids = {passage_id(p) for p in distinct}
    skipped = {passage_id(p) for p in passages} - ids
    if skipped:
        logger.warning(
            "lightrag: %d passage(s) repeat another's text and go in once: %s", len(skipped), _sample(skipped)
        )
    logger.info("lightrag: %d passages to index at concurrency %d", len(distinct), concurrency)
    started = time.monotonic()
    llm = GatewayLlm(client)
    async with open_rag(workdir, embedder, llm, concurrency=concurrency) as rag:
        for number in range(1, BUILD_PASSES + 1):
            stopped = not await _insert_pass(rag, distinct, llm, number)
            state = await _progress(rag, ids)
            if stopped or state.processed == len(distinct):
                break
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
        len(passages) - len(distinct),
        stopped,
    )


class LightRagProcedureSearch:
    def __init__(self, rag: Any, index_ref: str, passages: Sequence[Passage], llm: GatewayLlm) -> None:
        self.rag = rag
        self.llm = llm
        self.index_ref = index_ref
        self.passages = {passage_id(p): p for p in passages}

    async def search(self, request: ProcedureSearchRequest) -> ProcedureSearchResult:
        from lightrag import QueryParam

        if not request.redacted:
            raise UnredactedPayloadError(f"{PROGRAM_ID}: payload not marked redacted")
        param = QueryParam(mode="mix", chunk_top_k=CANDIDATES, enable_rerank=False)
        blocked = self.llm.blocked
        try:
            data = (await self.rag.aquery_data(request.query, param)).get("data") or {}
        except ModelBlockedError:  # one blocked claim isn't a failed run; anything else is raised
            logger.warning("lightrag: the gateway blocked a search, so it found nothing")
            return ProcedureSearchResult(index_ref=self.index_ref, vector_search=False, hits=())
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
                    score=round(1 / rank, 6),  # LightRAG gives an order, not a score
                    vector_rank=rank,
                )
            )
            if len(hits) == request.limit:
                break
        return ProcedureSearchResult(
            # A blocked keyword call raises nothing: LightRAG just finds less, or nothing
            index_ref=self.index_ref,
            vector_search=self.llm.blocked == blocked,
            hits=tuple(hits),
        )


def index_passages(store, index_ref: str) -> list[Passage]:
    """The same passages as a hybrid index, so both engines search one corpus."""
    raw = store.get(index_ref, classification=Classification.INTERNAL, authorization=_READER)
    return load_passages(store, ProcedureIndex.model_validate_json(raw).document_refs)
