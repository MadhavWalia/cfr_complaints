"""``procedure.search`` (LDR-0019): fusion, citations, per-document cap, blocks, and which versions are searched."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date

import numpy as np
import pytest

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.contracts import (
    Classification,
    ExtractionQuality,
    ProcedureDocument,
    ProcedureSearchRequest,
    ProcedureSection,
    ProcedureSnapshot,
    ProcedureSnapshotEntry,
    ProcedureStatus,
    canonical_json,
)
from cfr_v2.llm import FakeEmbeddingClient, UnredactedPayloadError
from cfr_v2.procedures import HybridProcedureSearch, build_index
from cfr_v2.procedures import __main__ as cli
from cfr_v2.procedures.search import KeywordIndex, Passage, tokens


def _document(key: str, sections: list[tuple[str, str]], *, name: str | None = None) -> ProcedureDocument:
    return ProcedureDocument(
        file_name=f"{key}.pdf",
        source_sha256=hashlib.sha256(key.encode()).hexdigest(),
        name=name or key,
        procedure_key=key,
        monthly_revision_date=date(2026, 9, 1),
        extractor_version="e",
        redaction_version="r",
        quality=ExtractionQuality(pages=9, method="contents"),
        sections=tuple(
            ProcedureSection(index=i, title=title, page_start=i + 3, page_end=i + 4, text=text)
            for i, (title, text) in enumerate(sections)
        ),
    )


def _snapshot(store, documents: dict[str, ProcedureDocument], superseded: tuple[str, ...] = ()) -> str:
    entries = []
    for number, (key, document) in enumerate(documents.items()):
        ref = store.put(canonical_json(document), media_type="application/json", classification=Classification.INTERNAL)
        entries.append(
            ProcedureSnapshotEntry(
                file_names=(document.file_name,),
                source_sha256=f"{number:064x}",
                source_ref=f"sha256:{number:064x}",
                document_ref=ref.metadata.ref,
                procedure_key=key,
                status=ProcedureStatus.SUPERSEDED if key in superseded else ProcedureStatus.CURRENT,
            )
        )
    snapshot = ProcedureSnapshot(source="manual", entries=tuple(entries))
    saved = store.put(canonical_json(snapshot), media_type="application/json", classification=Classification.INTERNAL)
    return saved.metadata.ref


def _request(query: str, **kwargs) -> ProcedureSearchRequest:
    return ProcedureSearchRequest(query=query, redacted=True, **kwargs)


def _corpus() -> dict[str, ProcedureDocument]:
    return {
        "Collections": _document(
            "Collections",
            [
                ("Payment plans", "Arrears payment plans for customers in hardship"),
                ("Late fees", "How late fees are waived after a missed payment"),
            ],
        ),
        "Authorizations": _document("Authorizations", [("Declines", "Why a card authorization was declined")]),
    }


@pytest.fixture
def setup():
    store = InMemoryArtifactStore()
    embedder = FakeEmbeddingClient()
    ref = _snapshot(store, _corpus())
    build = asyncio.run(build_index(store, ref, embedder))
    return store, embedder, build, HybridProcedureSearch.load(store, build.ref, embedder)


def test_the_best_passage_comes_first_and_can_be_cited(setup):
    _, _, build, search = setup
    result = asyncio.run(search.search(_request("late fees waived")))
    top = result.hits[0]
    assert (top.file_name, top.section_title, top.page_start, top.page_end) == ("Collections.pdf", "Late fees", 4, 5)
    assert top.monthly_revision_date == date(2026, 9, 1) and "waived" in top.text
    assert top.vector_rank == 1 and top.keyword_rank == 1
    assert result.index_ref == build.ref and result.vector_search


def test_the_procedure_name_helps_find_a_passage(setup):
    _, _, _, search = setup
    result = asyncio.run(search.search(_request("Authorizations")))
    assert result.hits[0].file_name == "Authorizations.pdf"


def test_a_passage_found_by_either_ranking_can_be_returned():
    index = KeywordIndex(["charge dispute", "unrelated words"])
    assert index.rank("disputes") == [0]
    passages = [Passage(d, s) for d in _corpus().values() for s in d.sections]
    vectors = np.zeros((3, 2), dtype=np.float32)
    vectors[2] = [1, 0]

    class Embedder(FakeEmbeddingClient):
        async def embed(self, texts, *, purpose, redacted):
            return [[1.0, 0.0]]

    search = HybridProcedureSearch("sha256:" + "0" * 64, passages, vectors, Embedder())
    result = asyncio.run(search.search(_request("hardship")))
    assert {h.section_title for h in result.hits} == {"Payment plans", "Declines"}
    by_title = {h.section_title: h for h in result.hits}
    assert by_title["Declines"].keyword_rank is None and by_title["Declines"].vector_rank == 1
    assert by_title["Payment plans"].vector_rank is None and by_title["Payment plans"].keyword_rank == 1


def test_one_document_cannot_fill_the_results():
    store, embedder = InMemoryArtifactStore(), FakeEmbeddingClient()
    big = _document("Big", [(f"Fees {i}", "fees fees fees") for i in range(10)])
    small = _document("Small", [("Fees", "a note about fees and other things")])
    build = asyncio.run(build_index(store, _snapshot(store, {"Big": big, "Small": small}), embedder))
    search = HybridProcedureSearch.load(store, build.ref, embedder)
    hits = asyncio.run(search.search(_request("fees", limit=5, max_per_document=2))).hits
    assert [h.file_name for h in hits].count("Big.pdf") == 2
    assert "Small.pdf" in [h.file_name for h in hits]


def test_the_limit_is_kept(setup):
    _, _, _, search = setup
    assert len(asyncio.run(search.search(_request("payment card fees", limit=1))).hits) == 1


def test_unrelated_words_find_nothing_by_keyword():
    assert KeywordIndex(["charge dispute"]).rank("zebra") == []


def test_superseded_versions_are_not_indexed():
    store, embedder = InMemoryArtifactStore(), FakeEmbeddingClient()
    documents = _corpus() | {"Old": _document("Old", [("Old rule", "an outdated rule")])}
    build = asyncio.run(build_index(store, _snapshot(store, documents, superseded=("Old",)), embedder))
    assert (build.procedures, build.passages) == (2, 3)
    search = HybridProcedureSearch.load(store, build.ref, embedder)
    assert all(h.file_name != "Old.pdf" for h in asyncio.run(search.search(_request("outdated rule"))).hits)


def test_a_blocked_section_is_still_found_by_keyword():
    store, embedder = InMemoryArtifactStore(), FakeEmbeddingClient(blocked={"hardship"})
    build = asyncio.run(build_index(store, _snapshot(store, _corpus()), embedder))
    assert build.blocked == 1
    embedder.blocked = set()
    search = HybridProcedureSearch.load(store, build.ref, embedder)
    hit = next(h for h in asyncio.run(search.search(_request("hardship"))).hits if h.section_title == "Payment plans")
    assert hit.vector_rank is None and hit.keyword_rank == 1


def test_a_blocked_query_falls_back_to_keywords_and_says_so(setup):
    _, embedder, _, search = setup
    embedder.blocked = {"late"}
    result = asyncio.run(search.search(_request("late fees")))
    assert not result.vector_search
    assert result.hits[0].section_title == "Late fees" and result.hits[0].vector_rank is None


def test_an_unredacted_query_is_refused(setup):
    _, _, _, search = setup
    with pytest.raises(UnredactedPayloadError):
        asyncio.run(search.search(ProcedureSearchRequest(query="late fees", redacted=False)))


def test_an_index_from_another_embedding_model_is_refused(setup):
    store, _, build, _ = setup
    other = FakeEmbeddingClient(dimensions=8)
    with pytest.raises(ValueError, match="built with"):
        HybridProcedureSearch.load(store, build.ref, other)


def test_the_same_snapshot_gives_the_same_index(setup):
    store, embedder, build, _ = setup
    again = asyncio.run(build_index(store, build.index.snapshot_ref, embedder))
    assert again.ref == build.ref


def test_tokens_drop_mask_tokens_and_plurals():
    assert tokens("Fees for ⟦PII⟧ address") == ["fee", "for", "address"]


def test_index_and_search_from_the_command_line(monkeypatch, capsys):
    store, embedder = InMemoryArtifactStore(max_bytes=10**8), FakeEmbeddingClient()
    registered = []
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(cli, "_embedder", lambda: embedder)
    monkeypatch.setattr(cli, "_register", registered.extend)
    monkeypatch.setattr(cli, "_masker", lambda: type("M", (), {"mask": lambda self, text: text})())
    assert cli.main(["index", _snapshot(store, _corpus())]) == 0
    printed = capsys.readouterr().out
    assert "2 procedures, 3 passages" in printed and len(registered) == 2
    ref = printed.split("index: ")[1].strip()
    assert cli.main(["search", ref, "late fees waived", "--limit", "1"]) == 0
    printed = capsys.readouterr().out
    assert "Collections.pdf / Late fees" in printed and "How late fees" not in printed
    assert cli.main(["search", ref, "late fees waived", "--text"]) == 0
    assert "How late fees" in capsys.readouterr().out
