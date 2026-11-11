"""LightRAG behind ``procedure.search`` (LDR-0019): building, resuming, recovering, searching and logging."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import re

import pytest

from cfr_v2.contracts import (
    ExtractionQuality,
    ProcedureDocument,
    ProcedureSearchRequest,
    ProcedureSection,
)
from cfr_v2.llm import FakeEmbeddingClient, FakeModelClient, ModelBlockedError, UnredactedPayloadError
from cfr_v2.procedures.lightrag_engine import GatewayLlm, passage_id
from cfr_v2.procedures.search import Passage

REF = "sha256:" + "0" * 64


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
            search = LightRagProcedureSearch(rag, REF, passages, llm)
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


class FlakyEmbeddings(FakeEmbeddingClient):
    """Fails its first calls, as a slow gateway does while LightRAG flushes its vectors."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    async def embed(self, texts, *, purpose, redacted):
        if self.failures > 0:
            self.failures -= 1
            raise TimeoutError("Embedding func: Worker execution timeout after 60s")
        return await super().embed(texts, purpose=purpose, redacted=redacted)


def test_a_failed_vector_flush_is_retried_and_logged_within_the_build(tmp_path, monkeypatch, caplog):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures import lightrag_engine

    monkeypatch.setattr(lightrag_engine, "INSERT_BATCH", 1)
    caplog.set_level(logging.INFO, logger=lightrag_engine.__name__)
    passages = _passages()
    build = asyncio.run(
        lightrag_engine.build_lightrag(
            tmp_path,
            passages,
            FlakyEmbeddings(failures=2),
            FakeModelClient(responder=_extraction),
            concurrency=2,
        )
    )
    assert (build.processed, build.failed, build.stopped) == (3, 0, False)
    counts = [int(n) for n in re.findall(r"\[(\d)/3\] passages in the graph", caplog.text)]
    assert counts == sorted(counts) and counts[-1] == 3  # never counts a failure as done
    assert re.search(
        r"lost \d passage\(s\) \((halted|timeout) \d.*\d queued; the next pass retries them: p-", caplog.text
    )
    assert re.search(r"pass 2 of 3: cleared [1-3] failed in \d+s, finished queued passages in \d+s", caplog.text)
    assert re.search(r"took \d+s; \d+ new model calls", caplog.text)
    assert "build ended after 0m: 3 of 3 in the graph" in caplog.text
    assert not any(p.section.text in caplog.text for p in passages)


def test_a_build_stops_when_nothing_gets_through(tmp_path, monkeypatch, caplog):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures import lightrag_engine

    monkeypatch.setattr(lightrag_engine, "INSERT_BATCH", 1)
    monkeypatch.setattr(lightrag_engine, "STUCK_BATCHES", 2)
    client, passages = FakeModelClient(responder=_extraction), _passages()
    build = asyncio.run(
        lightrag_engine.build_lightrag(tmp_path, passages, FlakyEmbeddings(failures=10**6), client, concurrency=2)
    )
    assert build.stopped and build.processed == 0
    assert re.search(r"stopping: 2 batches in a row added nothing \((halted|timeout) \d", caplog.text)
    assert re.search(r"still failing \((halted|timeout) \d", caplog.text)
    resumed = asyncio.run(
        lightrag_engine.build_lightrag(tmp_path, passages, FakeEmbeddingClient(), client, concurrency=2)
    )
    assert (resumed.processed, resumed.failed, resumed.stopped) == (3, 0, False)


def _copies(count):
    base = _passages()[0]
    return [
        dataclasses.replace(
            base, section=base.section.model_copy(update={"index": 100 + i, "text": f"{base.section.text} {i}"})
        )
        for i in range(count)
    ]


def test_a_halted_batch_leaves_nothing_behind(tmp_path):
    """The halt strands the rest of the batch as queued, and inserting those again makes duplicates."""
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import build_lightrag

    client = FakeModelClient(responder=_extraction)
    build = asyncio.run(build_lightrag(tmp_path, _copies(6), FlakyEmbeddings(failures=1), client, concurrency=1))
    assert (build.processed, build.failed, build.stopped) == (6, 0, False)
    rows = json.loads((tmp_path / "kv_store_doc_status.json").read_text())
    assert len(rows) == 6 and {r["status"] for r in rows.values()} == {"processed"}


def test_identical_passages_go_in_once(tmp_path):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import build_lightrag

    first, second = _passages()[:2]
    twin = dataclasses.replace(first, section=first.section.model_copy(update={"index": 99}))
    client = FakeModelClient(responder=_extraction)
    build = asyncio.run(build_lightrag(tmp_path, [first, twin, second], FakeEmbeddingClient(), client, concurrency=2))
    assert (build.passages, build.processed, build.failed, build.duplicates) == (2, 2, 0, 1)


class CountingModel(FakeModelClient):
    live = peak = 0

    async def complete(self, request):
        type(self).live += 1
        type(self).peak = max(type(self).peak, type(self).live)
        await asyncio.sleep(0.02)
        try:
            return await super().complete(request)
        finally:
            type(self).live -= 1


def test_concurrency_reaches_extraction(tmp_path):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import build_lightrag

    client = CountingModel(responder=_extraction)
    asyncio.run(build_lightrag(tmp_path, _copies(8), FakeEmbeddingClient(), client, concurrency=8))
    assert CountingModel.peak > 3  # LightRAG's default


class BlockingEmbeddings(FakeEmbeddingClient):
    """The firewall refuses any text with ``word``, as it could an entity description."""

    def __init__(self, word):
        super().__init__()
        self.word = word

    async def embed(self, texts, *, purpose, redacted):
        if any(self.word in t for t in texts):
            raise ModelBlockedError("blocked")
        return await super().embed(texts, purpose=purpose, redacted=redacted)


def test_a_blocked_embedding_costs_one_vector_not_the_build(tmp_path):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import LightRagProcedureSearch, build_lightrag, open_rag

    passages, client = _passages(), FakeModelClient(responder=_extraction)
    embedder = BlockingEmbeddings("Collections")

    async def run():
        build = await build_lightrag(tmp_path, passages, embedder, client, concurrency=2)
        embedder.word = "never sent"
        llm = GatewayLlm(client)
        async with open_rag(tmp_path, embedder, llm) as rag:
            search = LightRagProcedureSearch(rag, REF, passages, llm)
            return build, await search.search(ProcedureSearchRequest(query="late fees waived", redacted=True))

    build, found = asyncio.run(run())
    assert (build.processed, build.failed, build.stopped) == (3, 0, False) and build.blocked_embeddings > 0
    assert found.hits  # a NaN vector would have broken the ranking


def test_a_blocked_search_finds_nothing_and_says_so(tmp_path, caplog):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import LightRagProcedureSearch, build_lightrag, open_rag

    passages, client = _passages(), FakeModelClient(responder=_extraction)
    embedder = BlockingEmbeddings("fees")

    async def run():
        await build_lightrag(tmp_path, passages, FakeEmbeddingClient(), client, concurrency=2)
        llm = GatewayLlm(client)
        async with open_rag(tmp_path, embedder, llm) as rag:
            search = LightRagProcedureSearch(rag, REF, passages, llm)
            return await search.search(ProcedureSearchRequest(query="late fees waived", redacted=True))

    found = asyncio.run(run())
    assert found.blocked and found.hits == ()
    assert "the gateway blocked a search, so it found nothing" in caplog.text


def test_a_blocked_keyword_call_is_reported_too(tmp_path):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import LightRagProcedureSearch, build_lightrag, open_rag

    passages, embedder = _passages(), FakeEmbeddingClient()

    def blocks_keywords(request):
        if "high_level_keywords" in request.instructions + request.messages[-1].content:
            raise ModelBlockedError("blocked")
        return _extraction(request)

    async def run():
        client = FakeModelClient(responder=blocks_keywords)
        await build_lightrag(tmp_path, passages, embedder, client, concurrency=2)
        llm = GatewayLlm(client)
        async with open_rag(tmp_path, embedder, llm) as rag:
            search = LightRagProcedureSearch(rag, REF, passages, llm)
            query = "late fees were charged after the customer had already agreed a plan"
            return await search.search(ProcedureSearchRequest(query=query, redacted=True))

    assert asyncio.run(run()).blocked


def test_lightrags_logs_keep_their_place_but_lose_any_quoted_text():
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import NoTextInLightRagLogs

    def logged(message, *args):
        record = logging.LogRecord("lightrag", logging.ERROR, "operate.py", 4952, message, args, None)
        NoTextInLightRagLogs().filter(record)
        return record.getMessage()

    assert logged("JSON parsing error: %s; response: %r", "x", "the claim") == "message withheld (operate.py:4952)"
    assert logged("Merging stage failed in document 4/25: p-1") == "Merging stage failed in document 4/25: p-1"
    assert logged("[] Error embedding pending vector ops (upserts=27): x").startswith("[] Error embedding")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("Cancelled by internal error: NanoVectorDBStorage[entities]: x during merge", "halted"),
        ("procedures.lightrag: timeout after 4 tries", "timeout"),
        ("procedures.lightrag: blocked by the gateway", "blocked"),
        ("quotes the customer's words", "other"),
        (None, "other"),
    ],
)
def test_failure_reasons_are_fixed_words(error, reason):
    from cfr_v2.procedures.lightrag_engine import _reason

    assert _reason(error) == reason


def test_each_batch_is_a_trace_holding_its_flushes(tmp_path, monkeypatch, spans, metric_points):
    pytest.importorskip("lightrag")
    from cfr_v2.procedures import lightrag_engine

    monkeypatch.setattr(lightrag_engine, "INSERT_BATCH", 2)
    client = FakeModelClient(responder=_extraction)
    asyncio.run(lightrag_engine.build_lightrag(tmp_path, _copies(4), FakeEmbeddingClient(), client, concurrency=2))
    found = spans.get_finished_spans()
    batches = [s for s in found if s.name == "procedures.lightrag.batch"]
    flushes = [s for s in found if s.name == "procedures.lightrag.flush"]
    assert len(batches) == 2 and len({s.context.trace_id for s in batches}) == 2
    assert batches[-1].attributes["cfr.processed"] == 4 and batches[-1].attributes["cfr.outcome"] == "ok"
    assert {s.context.trace_id for s in flushes} == {s.context.trace_id for s in batches}
    points = metric_points()
    assert {a["stage"] for a, _ in points["cfr.procedures.stage.duration"]} >= {
        "batch",
        "flush",
        "model_call",
        "embedding",
    }
    assert ({"status": "processed"}, 4) in [(dict(a), v) for a, v in points["cfr.procedures.passages"]]


def test_tracing_flushes_is_safe_without_the_private_method():
    from types import SimpleNamespace

    from cfr_v2.procedures.lightrag_engine import _trace_flushes

    _trace_flushes(SimpleNamespace())


def test_lightrag_outlasts_the_gateway_clients_own_retries():
    from cfr_v2.llm import GatewaySettings
    from cfr_v2.llm.embeddings import EmbeddingSettings
    from cfr_v2.procedures.lightrag_engine import EMBEDDING_TIMEOUT_SEC, LLM_TIMEOUT_SEC, build_embedding_settings

    assert EMBEDDING_TIMEOUT_SEC >= build_embedding_settings(EmbeddingSettings()).call_deadline_sec
    assert LLM_TIMEOUT_SEC >= GatewaySettings().call_deadline_sec


def test_a_build_embeds_fewer_at_once_and_waits_longer_unless_told_to_be_stricter():
    from cfr_v2.llm.embeddings import EmbeddingSettings
    from cfr_v2.procedures.lightrag_engine import build_embedding_settings

    default = build_embedding_settings(EmbeddingSettings())
    assert (default.max_concurrency, default.call_deadline_sec) == (3, 1800)
    set_by_hand = build_embedding_settings(EmbeddingSettings(max_concurrency=1, call_deadline_sec=3600))
    assert (set_by_hand.max_concurrency, set_by_hand.call_deadline_sec) == (1, 3600)


def test_closing_lightrag_stops_its_call_workers(tmp_path):
    """Left running, they fail at exit with "Task was destroyed but it is pending"."""
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import build_lightrag

    async def run():
        await build_lightrag(
            tmp_path, _passages(), FakeEmbeddingClient(), FakeModelClient(responder=_extraction), concurrency=2
        )
        await asyncio.sleep(0)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(run()) == []


def test_stopping_workers_survives_a_shutdown_that_fails(caplog):
    from types import SimpleNamespace

    from cfr_v2.procedures.lightrag_engine import _stop_workers

    async def broken():
        raise RuntimeError("no")

    stopped = []

    async def fine():
        stopped.append(1)

    role = SimpleNamespace(shutdown=broken)
    rag = SimpleNamespace(
        role_llm_funcs={"extract": role, "query": role},
        llm_model_func=SimpleNamespace(shutdown=fine),
        embedding_func=SimpleNamespace(func=None),
    )
    asyncio.run(_stop_workers(rag))
    assert stopped == [1]
    assert caplog.text.count("could not stop LightRAG's call workers: RuntimeError") == 1


def test_passage_ids_are_stable_and_distinct():
    ids = [passage_id(p) for p in _passages()]
    assert len(set(ids)) == 3 and ids == [passage_id(p) for p in _passages()]


def test_a_build_records_its_snapshot_and_refuses_another(tmp_path):
    """LightRAG never removes a passage, so two snapshots in one store would mix (LDR-0021)."""
    pytest.importorskip("lightrag")
    from cfr_v2.procedures.lightrag_engine import (
        StoreMismatchError,
        build_lightrag,
        check_store,
        read_store_marker,
    )

    client, embedder = FakeModelClient(responder=_extraction), FakeEmbeddingClient()
    with pytest.raises(StoreMismatchError, match="no cfr_store.json"):
        check_store(tmp_path, REF)
    asyncio.run(build_lightrag(tmp_path, _passages(), embedder, client, concurrency=2, procedures_ref=REF))
    marker = read_store_marker(tmp_path)
    assert (marker["procedures_ref"], marker["passages"], marker["processed"], marker["failed"]) == (REF, 3, 3, 0)
    assert check_store(tmp_path, REF) == marker
    other = "sha256:" + "1" * 64
    with pytest.raises(StoreMismatchError, match="new directory"):
        asyncio.run(build_lightrag(tmp_path, _passages(), embedder, client, procedures_ref=other))
    with pytest.raises(StoreMismatchError, match="was built from"):
        check_store(tmp_path, other)


def test_importing_lightrag_leaves_logging_as_it_was():
    """nano-vectordb calls logging.basicConfig() on import, which printed every record twice in the
    worker. A fresh interpreter, as the import happens once per process."""
    pytest.importorskip("lightrag")
    import subprocess
    import sys

    code = (
        "import logging\n"
        "from cfr_v2.procedures.lightrag_engine import _import_lightrag\n"
        "_import_lightrag()\n"
        "root = logging.getLogger()\n"
        "print(len(root.handlers), root.level)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["0", str(logging.WARNING)]
