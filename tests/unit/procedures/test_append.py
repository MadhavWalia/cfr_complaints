"""Adding procedure documents (LDR-0043): a snapshot built by adding, and a store appended in place."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading

import pytest

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.contracts import ExtractionQuality, ProcedureDocument, ProcedureSection, ProcedureStatus
from cfr_v2.intake.fakes import RegexMasker
from cfr_v2.llm import FakeEmbeddingClient, FakeModelClient
from cfr_v2.procedures import ManualPdfSource, build_snapshot
from cfr_v2.procedures import __main__ as cli
from cfr_v2.procedures.append import (
    AppendRefused,
    StoreContents,
    check_base,
    finish,
    mark_appending,
    plan_append,
    read_store,
)
from cfr_v2.procedures.lightrag_engine import StoreMismatchError, check_store, passage_id
from cfr_v2.procedures.search import Passage
from cfr_v2.procedures.sources import ChosenPdfSource, ProcedureSourceError
from cfr_v2.procedures.stores import (
    APPENDING,
    STORE_MARKER,
    built_from,
    list_stores,
    read_marker,
    removable,
    store_name,
    write_marker,
)
from tests.unit.procedures.test_corpus import NAME, _procedure
from tests.unit.procedures.test_lightrag import _extraction

A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def _no_save_backoff(monkeypatch):
    from cfr_v2.procedures import lightrag_engine

    monkeypatch.setattr(lightrag_engine, "SAVE_BACKOFF_SEC", 0.0)


def _document(name: str, sections: list[tuple[str, str]], *, version: str = "") -> ProcedureDocument:
    return ProcedureDocument(
        file_name=f"{name}.pdf",
        source_sha256=hashlib.sha256((name + version).encode()).hexdigest(),
        name=name,
        procedure_key=name,
        extractor_version="e",
        redaction_version="r",
        quality=ExtractionQuality(pages=9, method="contents"),
        sections=tuple(
            ProcedureSection(index=i, title=t, page_start=i + 3, page_end=i + 4, text=x)
            for i, (t, x) in enumerate(sections)
        ),
    )


def _passages(*documents: ProcedureDocument) -> list[Passage]:
    return [Passage(d, s) for d in documents for s in d.sections]


COLLECTIONS = _document("Collections", [("Late fees", "Late fees are waived"), ("Plans", "Hardship payment plans")])
AUTH = _document("Auth", [("Declines", "Why a card was declined")])
DISPUTES = _document("Disputes", [("Chargebacks", "When a chargeback is raised")])
# The same PDF, extracted differently: same ids, one section's text changed
COLLECTIONS_REREAD = COLLECTIONS.model_copy(
    update={
        "sections": (
            COLLECTIONS.sections[0],
            COLLECTIONS.sections[1].model_copy(update={"text": "Hardship payment plans last six months"}),
        )
    }
)


# --- the comparison --------------------------------------------------------------------------


def test_passages_sort_into_unchanged_new_changed_and_gone():
    old = _passages(COLLECTIONS, AUTH)
    stored = StoreContents({passage_id(p): p.text for p in old}, frozenset({"dup-failed-row"}), 256)
    plan = plan_append(stored, _passages(COLLECTIONS_REREAD, DISPUTES))
    late, plans = (passage_id(p) for p in _passages(COLLECTIONS))
    assert plan.unchanged == (late,)
    assert plan.changed == (plans,)
    assert plan.new == (passage_id(_passages(DISPUTES)[0]),)
    assert plan.gone == tuple(sorted([passage_id(_passages(AUTH)[0]), "dup-failed-row"]))
    assert plan.deletes()[0] == plans  # changed ones go first
    assert plan.deletes(keep_gone=True) == [plans]
    assert plan.line() == "1 new · 2 gone · 1 changed · 1 unchanged"


def test_identical_texts_go_in_once_under_the_id_the_store_has():
    copy = _document("Auth", [("Declines", "Why a card was declined")], version="another file")
    stored = StoreContents({passage_id(_passages(AUTH)[0]): _passages(AUTH)[0].text}, frozenset(), None)
    plan = plan_append(stored, _passages(copy, AUTH))  # the copy comes first, but the store has AUTH's id
    assert [passage_id(p) for p in plan.passages] == [passage_id(_passages(AUTH)[0])]
    assert (plan.unchanged, plan.new, plan.gone, plan.duplicates) == ((passage_id(_passages(AUTH)[0]),), (), (), 1)


def test_a_passage_lightrag_has_no_text_for_counts_as_changed():
    pid = passage_id(_passages(AUTH)[0])
    plan = plan_append(StoreContents({pid: None}, frozenset(), None), _passages(AUTH))
    assert plan.changed == (pid,)


# --- the marker and the checks ---------------------------------------------------------------


def _marker(folder, **extra):
    folder.mkdir(exist_ok=True)
    write_marker(folder, {"procedures_ref": A, "program_version": "lightrag-1", **extra})


def test_a_store_part_way_through_an_append_is_searched_by_nothing(tmp_path):
    _marker(tmp_path, state=APPENDING, appending_to=B)
    assert built_from(tmp_path) is None
    with pytest.raises(StoreMismatchError, match="part-way through an append"):
        check_store(tmp_path, A)


def test_the_base_must_hold_the_snapshot_and_the_same_embedding_model(tmp_path):
    embedder = FakeEmbeddingClient()
    contents = StoreContents({}, frozenset(), 256)
    with pytest.raises(AppendRefused, match="the new snapshot is the one"):
        check_base(tmp_path, A, A, embedder, contents)
    _marker(tmp_path)
    with pytest.raises(AppendRefused, match="holds sha256:aaa"):
        check_base(tmp_path, B, A, embedder, contents)
    _, notes = check_base(tmp_path, A, B, embedder, contents)
    assert "doesn't record its embedding model" in notes[0]  # accepted with a note (Q8)
    with pytest.raises(AppendRefused, match="3072 dimensions"):
        check_base(tmp_path, A, B, embedder, StoreContents({}, frozenset(), 3072))
    _marker(tmp_path, embedding={"model": "another-model", "dimensions": 256})
    with pytest.raises(AppendRefused, match="embedded with another-model"):
        check_base(tmp_path, A, B, embedder, contents)
    _marker(tmp_path, state=APPENDING, appending_to="sha256:" + "c" * 64)
    with pytest.raises(AppendRefused, match="finish that one"):
        check_base(tmp_path, A, B, embedder, contents)
    _marker(tmp_path, state=APPENDING, appending_to=B)
    assert check_base(tmp_path, A, B, embedder, contents)[1]  # the same append: resumed


def test_a_rebuild_is_suggested_after_several_appends(tmp_path):
    _marker(
        tmp_path,
        embedding={"model": "fake-embedding"},
        lineage=[{"made": "build"}, *[{"made": "append"}] * 4],
    )
    _, notes = check_base(tmp_path, A, B, FakeEmbeddingClient(), StoreContents({}, frozenset(), 256))
    assert "this will be append 5" in notes[0]


def test_the_marker_is_replaced_whole(tmp_path):
    write_marker(tmp_path, {"procedures_ref": A})
    assert read_marker(tmp_path) == {"procedures_ref": A}
    assert not (tmp_path / (STORE_MARKER + ".part")).exists()


# --- with LightRAG ---------------------------------------------------------------------------


def _build(workdir, passages, embedder, client):
    from cfr_v2.procedures.lightrag_engine import build_lightrag

    return asyncio.run(build_lightrag(workdir, passages, embedder, client, concurrency=2, procedures_ref=A))


def _chunks_of(workdir) -> set[str]:
    graph = (workdir / "graph_chunk_entity_relation.graphml").read_text(encoding="utf-8")
    return {pid for d in (COLLECTIONS, AUTH, DISPUTES) for pid in map(passage_id, _passages(d)) if pid in graph}


def test_an_append_deletes_gone_replaces_changed_and_adds_new_in_place(tmp_path):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.append import append_lightrag
    from cfr_v2.procedures.lightrag_engine import GatewayLlm, LightRagProcedureSearch, open_rag

    embedder, client = FakeEmbeddingClient(), FakeModelClient(responder=_extraction)
    store = tmp_path / store_name(A)
    assert _build(store, _passages(COLLECTIONS, AUTH), embedder, client).processed == 3
    assert read_marker(store)["embedding"] == {"model": "fake-embedding", "dimensions": 256}
    contents = read_store(store)
    assert len(contents.processed) == 3 and contents.dimensions == 256
    marker, _ = check_base(store, A, B, embedder, contents)
    new_passages = _passages(COLLECTIONS_REREAD, DISPUTES)
    plan = plan_append(contents, new_passages)
    mark_appending(store, marker, B, plan, keep_gone=False)
    with pytest.raises(StoreMismatchError):
        check_store(store, A)

    outcome = asyncio.run(append_lightrag(store, plan, embedder, client, concurrency=2))
    assert outcome.complete and (outcome.deleted, outcome.delete_failed) == (2, {})
    assert (outcome.build.processed, outcome.build.passages, outcome.build.failed) == (3, 3, 0)
    finish(store, marker, B, outcome, embedder)
    done = read_marker(store)
    assert done["procedures_ref"] == B and done["state"] == "complete" and "appending_to" not in done
    assert done["lineage"][-1] | {"at": None, "started": None} == {
        "made": "append",
        "from": A,
        "procedures_ref": B,
        "at": None,
        "started": None,
        "new": 1,
        "gone": 1,
        "changed": 1,
        "unchanged": 1,
        "keep_gone": False,
        "failed": 0,
    }
    assert check_store(store, B)

    after = read_store(store)
    assert set(after.processed) == {passage_id(p) for p in new_passages}
    assert after.processed[passage_id(new_passages[1])] == new_passages[1].text  # the new text
    assert passage_id(_passages(AUTH)[0]) not in _chunks_of(store)

    async def search():
        llm = GatewayLlm(client)
        async with open_rag(store, embedder, llm) as rag:
            from cfr_v2.contracts import ProcedureSearchRequest

            found = await LightRagProcedureSearch(rag, B, new_passages, llm).search(
                ProcedureSearchRequest(query="late fees waived", redacted=True)
            )
        return {h.file_name for h in found.hits}

    assert asyncio.run(search()) <= {"Collections.pdf", "Disputes.pdf"}


def test_a_stopped_append_stays_closed_and_a_rerun_finishes_it(tmp_path):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.append import append_lightrag

    embedder, client = FakeEmbeddingClient(), FakeModelClient(responder=_extraction)
    store = tmp_path / "store"
    _build(store, _passages(COLLECTIONS, AUTH), embedder, client)
    contents = read_store(store)
    marker, _ = check_base(store, A, B, embedder, contents)
    plan = plan_append(contents, _passages(COLLECTIONS_REREAD, DISPUTES))
    mark_appending(store, marker, B, plan, keep_gone=False)
    stop = threading.Event()
    stop.set()  # asked before the first delete is done: it is saved, then the append ends
    first = asyncio.run(append_lightrag(store, plan, embedder, client, concurrency=2, save_every=8, stop=stop))
    assert first.asked_to_stop and not first.complete and first.deleted == 1
    assert read_marker(store)["state"] == APPENDING

    contents = read_store(store)
    marker, _ = check_base(store, A, B, embedder, contents)  # the same append: allowed
    again = plan_append(contents, _passages(COLLECTIONS_REREAD, DISPUTES))
    assert again.changed == () and len(again.new) == 2  # the changed one was deleted: it goes in as new
    mark_appending(store, marker, B, again, keep_gone=False)  # already appending: the first plan is kept
    assert read_marker(store)["append_planned"]["changed"] == 1
    outcome = asyncio.run(append_lightrag(store, again, embedder, client, concurrency=2))
    assert outcome.complete and outcome.build.processed == 3


def test_deletes_are_saved_in_batches_not_one_save_each(tmp_path, monkeypatch):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures import lightrag_engine
    from cfr_v2.procedures.append import append_lightrag

    embedder, client = FakeEmbeddingClient(), FakeModelClient(responder=_extraction)
    store = tmp_path / "store"
    _build(store, _passages(COLLECTIONS, AUTH, DISPUTES), embedder, client)
    contents = read_store(store)
    plan = plan_append(contents, _passages(DISPUTES))  # three to delete, nothing to insert
    saves = []
    real = lightrag_engine.BatchedSaves.save

    async def counted(self):
        saves.append(self.pending)
        await real(self)

    monkeypatch.setattr(lightrag_engine.BatchedSaves, "save", counted)
    outcome = asyncio.run(append_lightrag(store, plan, embedder, client, concurrency=2, save_every=64))
    assert outcome.deleted == 3 and outcome.complete
    assert saves[0] == 3  # one save for the three deletes
    assert set(read_store(store).processed) == {passage_id(_passages(DISPUTES)[0])}


# --- the CLI ---------------------------------------------------------------------------------


@pytest.fixture
def appending_cli(tmp_path, monkeypatch):
    """A built store under the cwd, as the worker finds it, and the CLI's outside world faked."""
    pytest.importorskip("lightrag")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CFR_LIGHTRAG_WORKDIR", raising=False)
    embedder, client = FakeEmbeddingClient(), FakeModelClient(responder=_extraction)
    _build(tmp_path / store_name(A), _passages(COLLECTIONS, AUTH), embedder, client)
    snapshots = {B: _passages(COLLECTIONS_REREAD, DISPUTES), A: _passages(COLLECTIONS, AUTH)}
    monkeypatch.setattr(cli, "_build_embedder", lambda: embedder)
    monkeypatch.setattr(cli, "_chat_client", lambda: client)
    monkeypatch.setattr(cli, "_store", lambda: None)
    monkeypatch.setattr("cfr_v2.procedures.search.corpus_passages", lambda store, ref: snapshots[ref])
    monkeypatch.setattr(cli, "queue_busy", lambda: None)
    return tmp_path


def test_the_preview_counts_and_changes_nothing(appending_cli, capsys):
    before = (appending_cli / store_name(A) / STORE_MARKER).read_text()
    assert cli.main(["lightrag-append", B, "--from", A, "--count-only"]) == 0
    printed = capsys.readouterr().out
    assert printed.strip().endswith("preview: 1 new · 1 gone · 1 changed · 1 unchanged")
    assert "about 6 gpt-5 calls" in printed and "late fees" not in printed.lower()
    assert (appending_cli / store_name(A) / STORE_MARKER).read_text() == before


def test_an_append_needs_in_place_and_an_idle_queue(appending_cli, monkeypatch, capsys):
    assert cli.main(["lightrag-append", B, "--from", A]) == 2
    err = capsys.readouterr().err
    assert "changes the store the current release uses" in err and "give --in-place" in err
    monkeypatch.setattr(cli, "queue_busy", lambda: "3 case run(s) are queued")
    assert cli.main(["lightrag-append", B, "--from", A, "--in-place"]) == 2
    assert "3 case run(s) are queued" in capsys.readouterr().err
    assert read_marker(appending_cli / store_name(A))["state"] == "complete"  # nothing changed


def test_an_append_renames_the_store_after_the_new_snapshot(appending_cli, capsys):
    assert cli.main(["lightrag-append", B, "--from", A, "--in-place", "--backup"]) == 0
    printed = capsys.readouterr().out
    assert printed.strip().endswith("3 of 3 passages in the graph; 0 failed")
    assert not (appending_cli / store_name(A)).exists()
    assert built_from(appending_cli / store_name(B)) == B
    backups = list(appending_cli.glob(f"{store_name(A)}.before-*"))
    assert len(backups) == 1 and built_from(backups[0]) == A
    assert cli.main(["lightrag-append", B, "--from", A, "--in-place"]) == 0
    assert "already appended" in capsys.readouterr().out


def test_stores_are_listed_and_only_unpinned_ones_removed(appending_cli, capsys):
    (appending_cli / "registry" / "releases").mkdir(parents=True)
    (appending_cli / "registry" / "releases" / "cfr-v2-x.yaml").write_text(f"procedure_snapshot: '{A}'\n")
    pinned = appending_cli / store_name(A)
    other = appending_cli / store_name(B)
    other.mkdir()
    write_marker(other, {"procedures_ref": B})
    (appending_cli / "lightrag_store_notastore").mkdir()
    found = {i.folder.name: i for i in list_stores(appending_cli, {})}
    assert set(found) == {pinned.name, other.name}
    assert removable(found[pinned.name]) == "pinned by cfr-v2-x" and removable(found[other.name]) is None
    assert cli.main(["stores"]) == 0 and "cfr-v2-x" in capsys.readouterr().out
    assert cli.main(["remove-store", pinned.name, "--yes"]) == 2 and pinned.exists()
    assert cli.main(["remove-store", "lightrag_store_notastore", "--yes"]) == 2
    assert cli.main(["remove-store", other.name]) == 0 and other.exists()  # without --yes: only says
    assert cli.main(["remove-store", other.name, "--yes"]) == 0 and not other.exists()


# --- a snapshot built by adding --------------------------------------------------------------


@pytest.fixture
def base(tmp_path):
    folder = tmp_path / "first"
    folder.mkdir()
    (folder / "Example Procedure Aug'26.pdf").write_bytes(_procedure("Example Procedure", "1 August 2026"))
    (folder / "Other Procedure.pdf").write_bytes(_procedure("Other Procedure", "1 September 2026"))
    store = InMemoryArtifactStore()
    first = build_snapshot(ManualPdfSource(folder), store=store, masker=RegexMasker([NAME]))
    return store, first


def test_added_pdfs_join_the_previous_snapshot_and_a_newer_version_replaces_its_own(tmp_path, base):
    store, first = base
    new = tmp_path / "new"
    new.mkdir()
    (new / "Example Procedure Sep'26.pdf").write_bytes(_procedure("Example Procedure", "1 September 2026"))
    (new / "Third Procedure.pdf").write_bytes(_procedure("Third Procedure", "1 September 2026"))
    (new / "Other Procedure.pdf").write_bytes(_procedure("Other Procedure", "1 September 2026"))  # already in
    source = ChosenPdfSource(sorted(new.iterdir()), dated=False)
    result = build_snapshot(source, store=store, masker=RegexMasker([NAME]), base=first.snapshot)
    status = {e.file_names[0]: e.status for e in result.snapshot.entries}
    assert status == {
        "Example Procedure Aug'26.pdf": ProcedureStatus.SUPERSEDED,
        "Example Procedure Sep'26.pdf": ProcedureStatus.CURRENT,
        "Other Procedure.pdf": ProcedureStatus.CURRENT,
        "Third Procedure.pdf": ProcedureStatus.CURRENT,
    }
    added = result.added
    assert added.added == ["Example Procedure Sep'26.pdf", "Third Procedure.pdf"]
    assert added.new_versions == ["Example Procedure Sep'26.pdf"] and added.already_in == ["Other Procedure.pdf"]
    assert result.reused == 2 and result.snapshot.source == "manual+added"


def test_a_previous_document_can_be_left_out_and_a_name_that_matches_nothing_is_refused(tmp_path, base):
    store, first = base
    new = tmp_path / "Third Procedure.pdf"
    new.write_bytes(_procedure("Third Procedure", "1 September 2026"))
    masker = RegexMasker([NAME])
    result = build_snapshot(
        ChosenPdfSource([new]), store=store, masker=masker, base=first.snapshot, leave_out=["other procedure.pdf"]
    )
    assert {e.file_names[0] for e in result.snapshot.entries} == {"Example Procedure Aug'26.pdf", "Third Procedure.pdf"}
    assert result.added.left_out == ["Other Procedure.pdf"]
    with pytest.raises(ValueError, match="nothing in the previous snapshot to leave out as: Missing"):
        build_snapshot(ChosenPdfSource([new]), store=store, masker=masker, base=first.snapshot, leave_out=["Missing"])


def test_a_chosen_file_must_be_a_pdf_by_its_bytes(tmp_path):
    fake = tmp_path / "renamed.pdf"
    fake.write_text("not a pdf")
    with pytest.raises(ProcedureSourceError, match="not a PDF: renamed.pdf"):
        ChosenPdfSource([fake]).list_documents()
    with pytest.raises(ProcedureSourceError, match="no such file"):
        ChosenPdfSource([tmp_path / "missing.pdf"]).list_documents()


def test_the_snapshot_cli_adds_prints_counts_and_deletes_the_uploads(tmp_path, base, monkeypatch, capsys):
    store, first = base
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(cli, "_masker", lambda: RegexMasker([NAME]))
    monkeypatch.setattr(cli, "_register", lambda stored: None)
    upload = tmp_path / "Third Procedure.pdf"
    upload.write_bytes(_procedure("Third Procedure", "1 September 2026"))
    argv = ["snapshot", "--add-to", first.ref, str(upload), "--undated", "--delete-after"]
    assert cli.main(argv) == 0
    printed = capsys.readouterr().out
    assert "added 1 · new versions of 0 · already in 0 · left out 0" in printed
    assert any(line.startswith("snapshot: sha256:") for line in printed.splitlines()) and not upload.exists()
    upload.write_bytes(_procedure("Third Procedure", "1 September 2026"))
    assert cli.main([*argv, "--leave-out", "nothing like it"]) == 2 and not upload.exists()  # deleted on a refusal too
    with pytest.raises(SystemExit):
        cli.main(["snapshot", "a", "b"])  # without --add-to: one folder
    json.dumps(printed)  # printable as is: names and counts only
