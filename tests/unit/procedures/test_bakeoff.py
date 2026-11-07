"""The bake-off (LDR-0019): claim choice, the blind workbook, scoring, and LightRAG behind ``procedure.search``."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys

import pytest
from openpyxl import load_workbook

from cfr_v2.contracts import (
    ExtractionQuality,
    ProcedureDocument,
    ProcedureHit,
    ProcedureSearchRequest,
    ProcedureSearchResult,
    ProcedureSection,
)
from cfr_v2.llm import FakeEmbeddingClient, FakeModelClient, ModelBlockedError, UnredactedPayloadError
from cfr_v2.procedures import __main__ as cli
from cfr_v2.procedures.bakeoff import (
    NOT_RELEVANT,
    PARTLY,
    RELEVANT,
    SHEET,
    BakeoffClaim,
    collect,
    load_claims,
    read_judgements,
    score,
    write_key,
    write_workbook,
)
from cfr_v2.procedures.lightrag_engine import GatewayLlm, passage_id
from cfr_v2.procedures.search import Passage

REF = "sha256:" + "0" * 64


class ByteTokens:
    """Stands in for tiktoken, which downloads its encodings on first use."""

    def encode(self, content):
        return list(content.encode())

    def decode(self, tokens):
        return bytes(tokens).decode(errors="replace")


@pytest.fixture(autouse=True)
def fresh_lightrag_state(monkeypatch):
    """No downloads, and a clean slate: LightRAG keeps its stores in process-wide state."""
    if importlib.util.find_spec("lightrag"):
        from lightrag.utils import Tokenizer

        monkeypatch.setattr(
            "lightrag.lightrag.TiktokenTokenizer", lambda *_: Tokenizer(model_name="bytes", tokenizer=ByteTokens())
        )
    yield
    if "lightrag.kg.shared_storage" in sys.modules:
        sys.modules["lightrag.kg.shared_storage"].finalize_share_data()


def _review(folder, case_id, statements, primary=None):
    claims = [{"claim_id": f"c{i}", "statement": s} for i, s in enumerate(statements)]
    payload = {"case_id": case_id, "claims": claims, "selection": {"claim_id": primary}}
    (folder / f"{case_id}.json").write_text(json.dumps(payload))


def test_every_case_gives_its_primary_claim_before_any_gives_a_second(tmp_path):
    _review(tmp_path, "A", ["a0", "a1", "a2", "a3"], primary="c2")
    _review(tmp_path, "B", ["b0"])
    _review(tmp_path, "C", ["c0", "c1"], primary="c1")
    claims = load_claims(tmp_path, 3)
    assert {c.statement for c in claims} == {"a2", "b0", "c1"}
    assert [c.claim_no for c in claims] == [1, 2, 3]


def test_claims_stop_at_the_count_and_skip_repeats_and_empty_cases(tmp_path):
    _review(tmp_path, "A", ["same", "other"])
    _review(tmp_path, "B", ["same"])
    _review(tmp_path, "C", [])
    assert sorted(c.statement for c in load_claims(tmp_path, 9)) == ["other", "same"]
    assert len(load_claims(tmp_path, 1)) == 1


def _hit(section: int, name: str = "Doc.pdf") -> ProcedureHit:
    return ProcedureHit(
        procedure_name=name,
        file_name=name,
        source_sha256=hashlib.sha256(name.encode()).hexdigest(),
        section_index=section,
        section_title=f"Section {section}",
        part=1,
        page_start=3,
        page_end=4,
        text=f"Passage {section}",
        score=0.1,
    )


class Scripted:
    def __init__(self, *hits):
        self.hits = hits

    async def search(self, request):
        assert request.redacted
        return ProcedureSearchResult(index_ref=REF, vector_search=True, hits=self.hits[: request.limit])


CLAIMS = [BakeoffClaim(1, "A", "Charged twice"), BakeoffClaim(2, "B", "Card declined")]


def _items():
    engines = {"hybrid": Scripted(_hit(1), _hit(2)), "lightrag": Scripted(_hit(2), _hit(3))}
    return asyncio.run(collect(CLAIMS, engines))


def test_a_passage_found_by_both_engines_is_judged_once():
    items = _items()
    assert len(items) == 6 and [i.item for i in items] == list(range(1, 7))
    shared = next(i for i in items if i.hit.section_index == 2)
    assert shared.ranks == {"hybrid": 2, "lightrag": 1}


def test_the_order_is_repeatable():
    assert [i.hit.section_index for i in _items()] == [i.hit.section_index for i in _items()]


def test_the_workbook_does_not_say_which_engine_found_what(tmp_path):
    path = tmp_path / "judgements.xlsx"
    write_workbook(_items(), path)
    book = load_workbook(path)
    text = " ".join(str(c.value) for row in book[SHEET].iter_rows() for c in row).lower()
    assert "lightrag" not in text and "hybrid" not in text
    assert "Charged twice" in " ".join(str(c.value) for c in book[SHEET]["C"])
    assert book[SHEET].data_validations.dataValidation[0].sqref.ranges
    assert set(read_judgements(path).values()) == {""}


def test_the_key_holds_no_text(tmp_path):
    path = tmp_path / "key.json"
    write_key(_items(), ("hybrid", "lightrag"), 5, path)
    raw = path.read_text()
    assert "Charged twice" not in raw and "Passage" not in raw


def _key() -> dict:
    def entry(claim, sha, **ranks):
        return {"claim_no": claim, "source_sha256": sha, "file_name": "d", "ranks": ranks}

    return {
        "engines": ["hybrid", "lightrag"],
        "top": 5,
        "items": {
            "1": entry(1, "x", hybrid=1),
            "2": entry(1, "y", hybrid=2, lightrag=1),
            "3": entry(1, "z", lightrag=2),
            "4": entry(2, "x", hybrid=1),
            "5": entry(2, "y", lightrag=1),
        },
    }


def test_scores_for_each_engine():
    answers = {1: NOT_RELEVANT, 2: RELEVANT, 3: RELEVANT, 4: PARTLY, 5: NOT_RELEVANT}
    report = score(answers, _key())
    hybrid, lightrag = report.scores
    assert (report.judged, report.unjudged, report.unknown_items) == (5, 0, 0)
    assert (hybrid.hit_rate, hybrid.precision, hybrid.partly_or_better) == (0.5, 0.333, 0.667)
    assert hybrid.first_relevant == 0.25 and hybrid.only_here == 0
    assert (lightrag.hit_rate, lightrag.precision, lightrag.first_relevant) == (0.5, 0.667, 0.5)
    assert lightrag.only_here == 1
    assert hybrid.procedures_per_claim == 1.5


def test_blank_answers_and_unknown_rows_are_counted_not_scored():
    report = score({1: RELEVANT, 2: "", 99: RELEVANT}, _key())
    assert (report.judged, report.unjudged, report.unknown_items) == (1, 4, 1)


def test_a_judged_workbook_scores_from_the_command_line(tmp_path, capsys):
    items = _items()
    book_path, key_path = tmp_path / "judgements.xlsx", tmp_path / "key.json"
    write_workbook(items, book_path)
    write_key(items, ("hybrid", "lightrag"), 5, key_path)
    book = load_workbook(book_path)
    for row in book[SHEET].iter_rows(min_row=2):
        row[7].value = RELEVANT
    book.save(book_path)
    assert cli.main(["bakeoff-score", str(book_path), str(key_path)]) == 0
    printed = capsys.readouterr().out
    assert "judged 6; not judged 0" in printed and "lightrag" in printed
    assert "Passage" not in printed and "Charged" not in printed


def test_the_llm_adapter_sends_redacted_turns_and_survives_a_block():
    client = FakeModelClient(responder=lambda request: {"text": "ok"})
    llm = GatewayLlm(client)
    history = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}]
    assert asyncio.run(llm("third", system_prompt="Extract", history_messages=history)) == "ok"
    request = client.requests[0]
    assert request.redacted and request.instructions == "Extract"
    assert [t.content for t in request.messages] == ["first", "second", "third"]

    def blocked(_):
        raise ModelBlockedError("blocked")

    llm = GatewayLlm(FakeModelClient(responder=blocked))
    assert asyncio.run(llm("text")) == ""
    assert (llm.calls, llm.blocked) == (1, 1)


def _passages() -> list[Passage]:
    def document(name, sections):
        return ProcedureDocument(
            file_name=f"{name}.pdf",
            source_sha256=hashlib.sha256(name.encode()).hexdigest(),
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

    documents = [
        document("Collections", [("Late fees", "Late fees are waived"), ("Plans", "Hardship payment plans")]),
        document("Auth", [("Declines", "Why a card was declined")]),
    ]
    return [Passage(d, s) for d in documents for s in d.sections]


def _extraction(request):
    prompt = request.messages[-1].content
    if "high_level_keywords" in request.instructions + prompt:
        return {"text": '{"high_level_keywords": ["fees"], "low_level_keywords": ["late fees"]}'}
    return {
        "text": "entity<|#|>Late Fees<|#|>concept<|#|>Fees for late payment\n"
        "entity<|#|>Collections<|#|>organization<|#|>A team\n"
        "relation<|#|>Collections<|#|>Late Fees<|#|>waive<|#|>Collections waives late fees\n<|COMPLETE|>"
    }


def test_lightrag_builds_resumes_and_cites_sections(tmp_path):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import LightRagProcedureSearch, build_lightrag, open_rag

    passages, embedder = _passages(), FakeEmbeddingClient()
    client = FakeModelClient(responder=_extraction)

    async def run():
        first = await build_lightrag(tmp_path, passages, embedder, client, concurrency=2)
        again = await build_lightrag(tmp_path, passages, embedder, client, concurrency=2)
        llm = GatewayLlm(client)
        async with open_rag(tmp_path, embedder, llm) as rag:
            search = LightRagProcedureSearch(rag, REF, passages)
            found = await search.search(ProcedureSearchRequest(query="late fees waived", redacted=True))
            with pytest.raises(UnredactedPayloadError):
                await search.search(ProcedureSearchRequest(query="late fees", redacted=False))
        return first, again, found

    first, again, found = asyncio.run(run())
    assert (first.processed, first.failed) == (3, 0) and first.model_calls >= 3
    assert (again.processed, again.failed, again.model_calls) == (3, 0, 0)
    top = found.hits[0]
    assert (top.file_name, top.section_title, top.page_start, top.page_end) == ("Collections.pdf", "Late fees", 3, 4)
    assert {h.section_title for h in found.hits} == {"Late fees", "Plans", "Declines"}


class Uncopyable(FakeModelClient):
    """Like the real gateway client: it holds locks and futures, which can't be deep-copied."""

    def __deepcopy__(self, memo):
        raise TypeError("cannot pickle '_asyncio.Future' object")


def test_lightrag_never_copies_the_model_client(tmp_path):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import build_lightrag

    client = Uncopyable(responder=_extraction)
    build = asyncio.run(build_lightrag(tmp_path, _passages(), FakeEmbeddingClient(), client, concurrency=2))
    assert (build.processed, build.failed) == (3, 0)


def test_a_failed_passage_is_retried_on_the_next_build(tmp_path):
    pytest.importorskip("lightrag")
    from cfr_v2.llm import ModelTimeoutError
    from cfr_v2.procedures.lightrag_engine import build_lightrag

    failing = {"on": True}

    def flaky(request):
        if failing["on"] and "Hardship" in request.messages[-1].content:
            raise ModelTimeoutError("gave up")
        return _extraction(request)

    client, embedder, passages = FakeModelClient(responder=flaky), FakeEmbeddingClient(), _passages()
    first = asyncio.run(build_lightrag(tmp_path, passages, embedder, client, concurrency=2))
    assert (first.processed, first.failed) == (2, 1)
    failing["on"] = False
    second = asyncio.run(build_lightrag(tmp_path, passages, embedder, client, concurrency=2))
    assert (second.processed, second.failed) == (3, 0)


def test_passage_ids_are_stable_and_distinct():
    ids = [passage_id(p) for p in _passages()]
    assert len(set(ids)) == 3 and ids == [passage_id(p) for p in _passages()]


def test_the_whole_bakeoff_from_the_command_line(tmp_path, monkeypatch, capsys):
    """The commands in order, on the model loop as on the Mac: a late failure would waste a long build."""
    pytest.importorskip("lightrag")
    from cfr_v2.artifacts import InMemoryArtifactStore
    from tests.unit.procedures.test_search import _corpus, _snapshot

    store, embedder = InMemoryArtifactStore(max_bytes=10**8), FakeEmbeddingClient()
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(cli, "_embedder", lambda: embedder)
    monkeypatch.setattr(cli, "_chat_client", lambda: FakeModelClient(responder=_extraction))
    monkeypatch.setattr(cli, "_register", lambda stored: None)
    assert cli.main(["index", _snapshot(store, _corpus())]) == 0
    index_ref = capsys.readouterr().out.split("index: ")[1].strip()

    workdir, claims, out = tmp_path / "store", tmp_path / "claims", tmp_path / "out"
    assert cli.main(["lightrag-build", index_ref, "--workdir", str(workdir), "--concurrency", "2"]) == 0
    assert "3 of 3 passages in the graph; 0 failed" in capsys.readouterr().out

    claims.mkdir()
    _review(claims, "A", ["late fees were not waived"])
    _review(claims, "B", ["card authorization declined"])
    args = ["bakeoff", index_ref, "--workdir", str(workdir), "--claims-from", str(claims), "--out", str(out)]
    assert cli.main([*args, "--claims", "2"]) == 0
    assert "2 claims" in capsys.readouterr().out
    key = json.loads((out / "key.json").read_text())
    assert {engine for entry in key["items"].values() for engine in entry["ranks"]} == {"hybrid", "lightrag"}

    book = load_workbook(out / "judgements.xlsx")
    for row in book[SHEET].iter_rows(min_row=2):
        row[7].value = RELEVANT
    book.save(out / "judgements.xlsx")
    assert cli.main(["bakeoff-score", str(out / "judgements.xlsx"), str(out / "key.json")]) == 0
    assert "not judged 0" in capsys.readouterr().out
