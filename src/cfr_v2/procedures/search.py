"""``procedure.search`` (LDR-0019): find procedure passages for a redacted query.

``ProcedureSearch`` is the capability; callers never depend on the engine behind it, so the
bake-off can swap engines. ``HybridProcedureSearch`` is the baseline: vector similarity and BM25
keyword ranking over the same passages, joined by reciprocal rank fusion. The corpus is small
enough to hold in memory: about 900 passages, 11 MB of vectors.

Each passage carries its procedure name and section title into both rankings, because some
procedures share their section titles (the three APR procedures, for example).
"""

from __future__ import annotations

import io
import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from cfr_v2.artifacts import StoredArtifact
from cfr_v2.contracts import (
    AuthorizationContext,
    Classification,
    ProcedureDocument,
    ProcedureHit,
    ProcedureIndex,
    ProcedureSearchRequest,
    ProcedureSearchResult,
    ProcedureSection,
    ProcedureStatus,
    canonical_json,
)
from cfr_v2.llm import EmbeddingClient, ModelBlockedError, embed_allowing_blocks

from .corpus import load_snapshot

BUILD_PURPOSE = "procedures.index"
QUERY_PURPOSE = "procedure.search"
RRF_K = 60  # the usual constant: rank 1 counts little more than rank 2, so neither ranking dominates
CANDIDATES = 50  # taken from each ranking before fusing
BM25_K1, BM25_B = 1.5, 0.75

_READER = AuthorizationContext(principal="procedure.search", purpose="load index")


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.replace("⟦PII⟧", " ").lower())
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]


@dataclass(frozen=True)
class Passage:
    document: ProcedureDocument
    section: ProcedureSection

    @property
    def text(self) -> str:
        return f"Procedure: {self.document.name}\nSection: {self.section.title}\n\n{self.section.text}"


class KeywordIndex:
    """BM25 over an inverted index."""

    def __init__(self, texts: Sequence[str]) -> None:
        self.count = len(texts)
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lengths: list[int] = []
        for i, text in enumerate(texts):
            words = tokens(text)
            self.lengths.append(len(words))
            for word, n in Counter(words).items():
                self.postings[word].append((i, n))
        self.average = sum(self.lengths) / self.count if self.count else 0.0

    def rank(self, query: str) -> list[int]:
        scores: dict[int, float] = defaultdict(float)
        for word in set(tokens(query)):
            found = self.postings.get(word, ())
            idf = math.log(1 + (self.count - len(found) + 0.5) / (len(found) + 0.5))
            for i, n in found:
                shape = 1 - BM25_B + BM25_B * self.lengths[i] / self.average
                scores[i] += idf * n * (BM25_K1 + 1) / (n + BM25_K1 * shape)
        return sorted(scores, key=lambda i: (-scores[i], i))


class ProcedureSearch(Protocol):
    async def search(self, request: ProcedureSearchRequest) -> ProcedureSearchResult: ...


def load_passages(store, refs: Sequence[str]) -> list[Passage]:
    passages = []
    for ref in refs:
        raw = store.get(ref, classification=Classification.INTERNAL, authorization=_READER)
        document = ProcedureDocument.model_validate_json(raw)
        passages += [Passage(document, section) for section in document.sections]
    return passages


class HybridProcedureSearch:
    def __init__(
        self, index_ref: str, passages: Sequence[Passage], vectors: np.ndarray, embedder: EmbeddingClient
    ) -> None:
        if len(passages) != len(vectors):
            raise ValueError(f"{len(passages)} passages but {len(vectors)} vectors")
        self.index_ref = index_ref
        self.passages = list(passages)
        self.vectors = vectors  # rows are unit length, or zero
        self.embedder = embedder
        self.keywords = KeywordIndex([p.text for p in passages])

    @classmethod
    def load(cls, store, ref: str, embedder: EmbeddingClient) -> HybridProcedureSearch:
        raw = store.get(ref, classification=Classification.INTERNAL, authorization=_READER)
        index = ProcedureIndex.model_validate_json(raw)
        if (index.embedding_model, index.dimensions) != (embedder.model_id, embedder.dimensions):
            raise ValueError(f"the index was built with {index.embedding_model}, {index.dimensions} dimensions")
        data = store.get(index.vectors_ref, classification=Classification.INTERNAL, authorization=_READER)
        vectors = np.load(io.BytesIO(data), allow_pickle=False)
        return cls(ref, load_passages(store, index.document_refs), vectors, embedder)

    async def _vector_rank(self, request: ProcedureSearchRequest) -> list[int] | None:
        try:
            [vector] = await self.embedder.embed([request.query], purpose=QUERY_PURPOSE, redacted=request.redacted)
        except ModelBlockedError:
            return None
        query = np.asarray(vector, dtype=np.float32)
        query /= np.linalg.norm(query) or 1.0
        similarity = self.vectors @ query
        usable = np.flatnonzero(self.vectors.any(axis=1))
        return [int(i) for i in usable[np.argsort(-similarity[usable], kind="stable")]]

    async def search(self, request: ProcedureSearchRequest) -> ProcedureSearchResult:
        by_vector = await self._vector_rank(request)  # refuses unredacted text before anything else happens
        by_keyword = self.keywords.rank(request.query)
        vector_rank = {i: r for r, i in enumerate((by_vector or [])[:CANDIDATES], start=1)}
        keyword_rank = {i: r for r, i in enumerate(by_keyword[:CANDIDATES], start=1)}
        scores: dict[int, float] = defaultdict(float)
        for ranks in (vector_rank, keyword_rank):
            for i, rank in ranks.items():
                scores[i] += 1 / (RRF_K + rank)
        per_document: Counter[str] = Counter()
        hits: list[ProcedureHit] = []
        for i in sorted(scores, key=lambda i: (-scores[i], i)):
            passage = self.passages[i]
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
                    score=round(scores[i], 6),
                    vector_rank=vector_rank.get(i),
                    keyword_rank=keyword_rank.get(i),
                )
            )
            if len(hits) == request.limit:
                break
        return ProcedureSearchResult(index_ref=self.index_ref, vector_search=by_vector is not None, hits=tuple(hits))


@dataclass
class IndexBuild:
    ref: str
    index: ProcedureIndex
    passages: int
    blocked: int  # passages the gateway refused: keyword search only
    procedures: int
    stored: list[StoredArtifact] = field(default_factory=list)  # to register


async def build_index(store, snapshot_ref: str, embedder: EmbeddingClient) -> IndexBuild:
    """Index the CURRENT procedures of a snapshot. Superseded versions are never searched."""
    snapshot = load_snapshot(store, snapshot_ref)
    refs = [e.document_ref for e in snapshot.entries if e.status == ProcedureStatus.CURRENT]
    passages = load_passages(store, refs)
    vectors = await embed_allowing_blocks(
        embedder, [p.text for p in passages], purpose=BUILD_PURPOSE, redacted=True
    )
    matrix = np.zeros((len(passages), embedder.dimensions), dtype=np.float32)
    for row, vector in enumerate(vectors):
        if vector is not None:
            matrix[row] = vector
            matrix[row] /= np.linalg.norm(matrix[row]) or 1.0
    buffer = io.BytesIO()
    np.save(buffer, matrix, allow_pickle=False)
    saved = store.put(
        buffer.getvalue(), media_type="application/octet-stream", classification=Classification.INTERNAL
    )
    index = ProcedureIndex(
        snapshot_ref=snapshot_ref,
        embedding_model=embedder.model_id,
        dimensions=embedder.dimensions,
        vectors_ref=saved.metadata.ref,
        document_refs=tuple(refs),
    )
    stored = store.put(canonical_json(index), media_type="application/json", classification=Classification.INTERNAL)
    return IndexBuild(
        ref=stored.metadata.ref,
        index=index,
        passages=len(passages),
        blocked=sum(v is None for v in vectors),
        procedures=len(refs),
        stored=[saved, stored],
    )
