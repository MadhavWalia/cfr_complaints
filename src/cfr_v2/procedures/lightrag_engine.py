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
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
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

from .search import _READER, CANDIDATES, Passage, load_passages

PROGRAM_ID = "procedures.lightrag"
PROGRAM_VERSION = "lightrag-1"
BUILD_PURPOSE = "procedures.lightrag.index"
CHUNK_TOKENS = 2048  # above the largest passage, so a passage stays one chunk
EMBEDDING_TOKEN_LIMIT = 8192
LLM_TIMEOUT_SEC = 1200  # as the gateway client's deadline: it does the retrying
INSERT_BATCH = 25
DEFAULT_INSTRUCTIONS = "Follow the instructions in the message."


def passage_id(passage: Passage) -> str:
    return f"p-{passage.document.source_sha256[:16]}-{passage.section.index:04d}"


class GatewayLlm:
    """LightRAG's model function over our client. A blocked call gives an empty answer, so one
    firewall block costs that chunk's graph entries, not the build."""

    def __init__(self, client: ModelClient) -> None:
        self.client = client
        self.calls = 0
        self.blocked = 0

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
        try:
            response = await self.client.complete(request)
        except ModelBlockedError:
            self.blocked += 1
            return ""
        return str(response.output["text"])


def _embedding_func(embedder: EmbeddingClient):
    from lightrag.utils import EmbeddingFunc

    async def embed(texts: list[str], **_: Any) -> np.ndarray:
        vectors = await embedder.embed(texts, purpose=BUILD_PURPOSE, redacted=True)
        return np.asarray(vectors, dtype=np.float32)

    return EmbeddingFunc(embedding_dim=embedder.dimensions, max_token_size=EMBEDDING_TOKEN_LIMIT, func=embed)


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
        embedding_func=_embedding_func(embedder),
        chunk_token_size=CHUNK_TOKENS,
        chunk_overlap_token_size=0,
        llm_model_max_async=concurrency,
        default_llm_timeout=LLM_TIMEOUT_SEC,
    )
    for name in ("lightrag", "nano-vectordb"):  # at INFO they print entity names and queries
        logging.getLogger(name).setLevel(logging.ERROR)
    await rag.initialize_storages()
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


async def _counts(rag: Any) -> tuple[int, int]:
    from lightrag.base import DocStatus

    counts = await rag.doc_status.get_status_counts()
    return counts.get(DocStatus.PROCESSED.value, 0), counts.get(DocStatus.FAILED.value, 0)


async def build_lightrag(
    workdir: Path,
    passages: Sequence[Passage],
    embedder: EmbeddingClient,
    client: ModelClient,
    *,
    concurrency: int = 16,
    progress: Callable[[int, int], None] | None = None,
) -> LightRagBuild:
    """Passages already processed are skipped, so an interrupted build can resume. Failed ones are
    cleared first and tried again: LightRAG would treat a second insert as a duplicate."""
    from lightrag.base import DocStatus

    llm = GatewayLlm(client)
    async with open_rag(workdir, embedder, llm, concurrency=concurrency) as rag:
        for doc_id in await rag.doc_status.get_docs_by_statuses([DocStatus.FAILED]):
            await rag.adelete_by_doc_id(doc_id)
        done = await rag.doc_status.get_docs_by_statuses([DocStatus.PROCESSED])
        waiting = [p for p in passages if passage_id(p) not in done]
        for start in range(0, len(waiting), INSERT_BATCH):
            batch = waiting[start : start + INSERT_BATCH]
            ids = [passage_id(p) for p in batch]
            await rag.ainsert([p.text for p in batch], ids=ids, file_paths=ids)
            if progress:
                progress(len(passages) - len(waiting) + start + len(batch), len(passages))
        processed, failed = await _counts(rag)
    return LightRagBuild(len(passages), processed, failed, llm.calls, llm.blocked)


class LightRagProcedureSearch:
    def __init__(self, rag: Any, index_ref: str, passages: Sequence[Passage]) -> None:
        self.rag = rag
        self.index_ref = index_ref
        self.passages = {passage_id(p): p for p in passages}

    async def search(self, request: ProcedureSearchRequest) -> ProcedureSearchResult:
        from lightrag import QueryParam

        if not request.redacted:
            raise UnredactedPayloadError(f"{PROGRAM_ID}: payload not marked redacted")
        param = QueryParam(mode="mix", chunk_top_k=CANDIDATES, enable_rerank=False)
        data = (await self.rag.aquery_data(request.query, param)).get("data") or {}
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
        return ProcedureSearchResult(index_ref=self.index_ref, vector_search=True, hits=tuple(hits))


def index_passages(store, index_ref: str) -> list[Passage]:
    """The same passages as a hybrid index, so both engines search one corpus."""
    raw = store.get(index_ref, classification=Classification.INTERNAL, authorization=_READER)
    return load_passages(store, ProcedureIndex.model_validate_json(raw).document_refs)
