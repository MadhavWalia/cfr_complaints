"""``procedure.search`` (LDR-0019): which passages are searched, and the commands that build and search LightRAG."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.contracts import (
    AuthorizationContext,
    Classification,
    ExtractionQuality,
    ProcedureDocument,
    ProcedureSection,
    ProcedureSnapshot,
    ProcedureSnapshotEntry,
    ProcedureStatus,
    canonical_json,
)
from cfr_v2.llm import FakeEmbeddingClient, FakeModelClient
from cfr_v2.procedures import __main__ as cli
from cfr_v2.procedures import corpus_passages
from cfr_v2.procedures.stores import store_name

_ANY = AuthorizationContext(principal="test", purpose="check")


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


def _titles(passages) -> list[str]:
    return [f"{p.document.name}/{p.section.title}" for p in passages]


def test_only_current_procedures_are_searched():
    store = InMemoryArtifactStore()
    corpus = {**_corpus(), "Old": _document("Old", [("Fees", "The old fee rules")])}
    passages = corpus_passages(store, _snapshot(store, corpus, superseded=("Old",)))
    assert _titles(passages) == ["Collections/Payment plans", "Collections/Late fees", "Authorizations/Declines"]
    assert (
        passages[1].text
        == "Procedure: Collections\nSection: Late fees\n\nHow late fees are waived after a missed payment"
    )


def test_an_index_from_the_old_index_command_gives_the_same_passages():
    """Stores built from one keep working: the passage ids are the same."""
    store = InMemoryArtifactStore()
    snapshot = _snapshot(store, _corpus())
    refs = [
        e["document_ref"]
        for e in json.loads(store.get(snapshot, classification=Classification.INTERNAL, authorization=_ANY))["entries"]
    ]
    old = {
        "snapshot_ref": snapshot,
        "embedding_model": "m",
        "dimensions": 3,
        "vectors_ref": snapshot,
        "document_refs": refs,
    }
    index = store.put(json.dumps(old).encode(), media_type="application/json", classification=Classification.INTERNAL)
    assert _titles(corpus_passages(store, index.metadata.ref)) == _titles(corpus_passages(store, snapshot))


def test_anything_else_is_refused():
    store = InMemoryArtifactStore()
    ref = store.put(b"{}", media_type="application/json", classification=Classification.INTERNAL).metadata.ref
    with pytest.raises(ValueError, match="not a procedure snapshot"):
        corpus_passages(store, ref)


def test_build_then_search_from_the_command_line(tmp_path, monkeypatch, capsys):
    """Both commands in order, on the model loop as on the Mac: a late failure would waste a long build."""
    pytest.importorskip("lightrag")
    from tests.unit.procedures.test_lightrag import _extraction

    store, embedder = InMemoryArtifactStore(max_bytes=10**8), FakeEmbeddingClient()
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(cli, "_embedder", lambda *_: embedder)
    monkeypatch.setattr(cli, "_chat_client", lambda: FakeModelClient(responder=_extraction))
    monkeypatch.setattr(cli, "_masker", lambda: type("M", (), {"mask": lambda self, text: text})())
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CFR_LIGHTRAG_WORKDIR", raising=False)
    ref = _snapshot(store, _corpus())
    assert cli.main(["lightrag-build", ref, "--concurrency", "2"]) == 0  # into the snapshot's own folder
    printed = capsys.readouterr().out
    assert f"store: {store_name(ref)}" in printed and "3 of 3 passages in the graph; 0 failed" in printed
    assert (tmp_path / store_name(ref)).is_dir()
    assert cli.main(["search", ref, "late fees waived", "--limit", "1"]) == 0
    printed = capsys.readouterr().out
    assert "Collections.pdf / Late fees" in printed and "How late fees" not in printed
    workdir = str(tmp_path / store_name(ref))
    assert cli.main(["search", ref, "late fees waived", "--workdir", workdir, "--text"]) == 0
    assert "How late fees" in capsys.readouterr().out
    # a store holds one snapshot: another ref is refused, before any model call
    other = "sha256:" + "e" * 64
    assert cli.main(["search", other, "late fees", "--workdir", workdir]) == 2
    assert "was built from" in capsys.readouterr().err
