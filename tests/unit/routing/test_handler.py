"""The ``c04.routing`` stage (LDR-0021): input, result, failure policy, pinned resources, and LightRAG
in one worker process."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack

import pytest

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.contracts import (
    ClaimRoutingOutcome,
    Classification,
    ProcedureSearchResult,
    ReleaseManifest,
    RoutingResult,
)
from cfr_v2.llm import FakeModelClient, ModelLoop, ModelTimeoutError
from cfr_v2.llm.dspy_lm import gateway_lm
from cfr_v2.routing import ROUTE_VERSION
from cfr_v2.routing import __main__ as cli
from cfr_v2.routing.handler import (
    PinnedRouting,
    RoutingContext,
    RoutingSetupError,
    lightrag_opener,
    make_routing_handler,
)
from cfr_v2.runtime.stages import STAGES, StageInput
from tests.unit.procedures.conftest import fresh_lightrag_state  # noqa: F401 - no downloads, clean LightRAG state
from tests.unit.routing.test_router import CATALOG, DOCUMENT, SNAPSHOT, Search, _hit, _router_reply
from tests.unit.selection.test_selector import _claim_set

ROUTING = next(s for s in STAGES if s.logical_task_id == "c04.routing")
MANIFEST_REF = "cfr-v2-test@abc"


@pytest.fixture
def loop():
    lp = ModelLoop()
    yield lp
    lp.close()


def _put(store, data: bytes) -> str:
    return store.put(data, media_type="application/json", classification=Classification.INTERNAL).metadata.ref


def _handle(store, ref, client, resources, loop) -> RoutingResult:
    handler = make_routing_handler(store, gateway_lm(client, "fake-model"), resources, loop=loop)
    inp = StageInput("run-1", ROUTING, "request-ref-unused", ("p1", ref, "sel"), "t4", MANIFEST_REF)
    return RoutingResult.model_validate_json(handler(inp).content)


def _context(search) -> RoutingContext:
    return RoutingContext(CATALOG, search, SNAPSHOT)


def test_the_stage_routes_every_claim_from_the_claim_set_only(loop):
    store, claim_set = InMemoryArtifactStore(), _claim_set()
    ref = _put(store, claim_set.model_dump_json().encode())
    client, search, asked = FakeModelClient(responder=_router_reply), Search([_hit(1, 1, 1)]), []

    def resources(manifest_ref):
        asked.append(manifest_ref)
        return _context(search)

    result = _handle(store, ref, client, resources, loop)
    assert ROUTING.program_version == ROUTE_VERSION and asked == [MANIFEST_REF]
    assert (result.claim_set_hash, result.procedures_ref, result.verticals_sha256) == (
        claim_set.claim_set_hash,
        SNAPSHOT,
        CATALOG.sha256,
    )
    assert [(c.claim_id, c.outcome, c.candidates[0].vertical_id) for c in result.claims] == [
        ("c1", ClaimRoutingOutcome.ROUTED, "HOLD"),
        ("c2", ClaimRoutingOutcome.ROUTED, "HOLD"),
    ]
    assert sorted(r.query for r in search.requests) == sorted(c.statement for c in claim_set.claims)
    assert len(client.requests) == 2 and {r.trace_id for r in client.requests} == {"t4"}
    sent = "".join(t.content for r in client.requests for t in r.messages)
    assert "Customer says the hotel charged twice" in sent  # c1's CLAIM allegation
    for secret in ("IMPACT-SECRET", "EVIDENCE-SECRET", "STATEMENT-SECRET", "CHECK-SECRET"):
        assert secret not in sent


def test_one_failed_claim_fails_the_stage(loop):
    store = InMemoryArtifactStore()
    ref = _put(store, _claim_set().model_dump_json().encode())

    class Failing(Search):
        async def search(self, request):
            if "rude" in request.query:
                raise ModelTimeoutError("gave up")
            return await super().search(request)

    with pytest.raises(ModelTimeoutError):
        _handle(store, ref, FakeModelClient(responder=_router_reply), lambda _: _context(Failing()), loop)


def test_a_blocked_search_routes_the_claim_without_sections(loop):
    store = InMemoryArtifactStore()
    ref = _put(store, _claim_set().model_dump_json().encode())

    class Blocked(Search):
        async def search(self, request):
            return ProcedureSearchResult(procedures_ref=SNAPSHOT, blocked=True, hits=())

    result = _handle(store, ref, FakeModelClient(responder=_router_reply), lambda _: _context(Blocked()), loop)
    assert all(c.sections_seen == () and c.outcome is ClaimRoutingOutcome.ROUTED for c in result.claims)


# --- pinned resources ----------------------------------------------------------------------


def _manifest(store, **pins) -> ReleaseManifest:
    return ReleaseManifest.build(release_id="cfr-v2-test", **pins)


def _verticals(store) -> str:
    return store.put(DOCUMENT.encode(), media_type="text/markdown", classification=Classification.INTERNAL).metadata.ref


class Opener:
    def __init__(self):
        self.opened, self.closed = [], []

    async def __call__(self, procedures_ref, stack: AsyncExitStack):
        self.opened.append(procedures_ref)

        async def close():
            self.closed.append(procedures_ref)

        stack.push_async_callback(close)
        return Search()


def test_a_release_without_routing_pins_cannot_route(loop):
    store = InMemoryArtifactStore()
    pinned = PinnedRouting(store, lambda ref: _manifest(store), Opener(), loop=loop)
    with pytest.raises(RoutingSetupError, match="pins no routing_verticals"):
        pinned(MANIFEST_REF)
    with pytest.raises(RoutingSetupError, match="no release manifest"):
        pinned(None)


def test_the_pinned_document_and_one_store_serve_every_run(loop):
    store, opener = InMemoryArtifactStore(), Opener()
    verticals = _verticals(store)
    manifest = _manifest(store, routing_verticals=verticals, procedure_snapshot=SNAPSHOT)
    other = _manifest(store, routing_verticals=verticals, procedure_snapshot="sha256:" + "c" * 64)
    pinned = PinnedRouting(store, lambda ref: other if ref == "other" else manifest, opener, loop=loop)

    first, second = pinned(MANIFEST_REF), pinned(MANIFEST_REF)
    assert first.catalog == CATALOG and first.procedures_ref == SNAPSHOT and first.search is second.search
    assert verticals == "sha256:" + CATALOG.sha256  # the result's verticals_sha256 names the artifact
    assert opener.opened == [SNAPSHOT]
    with pytest.raises(RoutingSetupError, match="serves"):
        pinned("other")
    pinned.close()
    assert opener.closed == [SNAPSHOT]
    pinned.close()  # twice is harmless


def test_no_store_directory_or_a_store_from_another_snapshot_is_refused(tmp_path, loop):
    from cfr_v2.procedures.lightrag_engine import STORE_MARKER, StoreMismatchError

    store = InMemoryArtifactStore()
    verticals = _verticals(store)
    manifest = _manifest(store, routing_verticals=verticals, procedure_snapshot=SNAPSHOT)

    def pinned(workdir):
        return PinnedRouting(store, lambda _: manifest, lightrag_opener(store, workdir, None, None), loop=loop)

    with pytest.raises(RoutingSetupError, match="CFR_LIGHTRAG_WORKDIR"):
        pinned(None)(MANIFEST_REF)
    with pytest.raises(StoreMismatchError, match="no cfr_store.json"):
        pinned(tmp_path)(MANIFEST_REF)
    (tmp_path / STORE_MARKER).write_text(json.dumps({"procedures_ref": "sha256:" + "d" * 64}))
    with pytest.raises(StoreMismatchError, match="was built from"):
        pinned(tmp_path)(MANIFEST_REF)


# --- publish-verticals ---------------------------------------------------------------------


def test_publish_verticals_stores_the_document_and_prints_refs_only(tmp_path, monkeypatch, capsys):
    store, registered = InMemoryArtifactStore(), []
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(cli, "_register", registered.append)
    path = tmp_path / "knowledge_document.txt"
    path.write_text(DOCUMENT, encoding="utf-8")

    assert cli.main(["publish-verticals", str(path)]) == 0
    ref = "sha256:" + CATALOG.sha256
    [stored] = registered
    assert stored.metadata.ref == ref and stored.metadata.classification is Classification.INTERNAL
    out = capsys.readouterr().out
    assert f"routing_verticals: {ref}" in out and "3 verticals" in out
    assert "RULES-MARKER" not in out and "payment hold" not in out


def test_publish_verticals_refuses_a_document_without_verticals(tmp_path, monkeypatch):
    from cfr_v2.routing import VerticalDocumentError

    monkeypatch.setattr(cli, "_store", lambda: pytest.fail("nothing stored"))
    path = tmp_path / "other.txt"
    path.write_text("not the knowledge document", encoding="utf-8")
    with pytest.raises(VerticalDocumentError):
        cli.main(["publish-verticals", str(path)])


# --- LightRAG in a worker process ----------------------------------------------------------


def _responder(request):
    if request.program_id == "c04.route":
        return _router_reply(request)
    from tests.unit.procedures.test_lightrag import _extraction

    return _extraction(request)


def test_two_cases_routed_over_one_lightrag_store_then_closed(tmp_path, loop):
    """The store is opened once on the model loop, serves both cases, and closes cleanly."""
    pytest.importorskip("lightrag")
    from cfr_v2.llm import FakeEmbeddingClient
    from cfr_v2.procedures import corpus_passages
    from cfr_v2.procedures.lightrag_engine import build_lightrag, read_store_marker
    from tests.unit.procedures.test_search import _corpus, _snapshot

    store, embedder = InMemoryArtifactStore(max_bytes=10**8), FakeEmbeddingClient()
    client = FakeModelClient(responder=_responder)
    snapshot, workdir = _snapshot(store, _corpus()), tmp_path / "store"
    build = loop.run(
        build_lightrag(workdir, corpus_passages(store, snapshot), embedder, client, procedures_ref=snapshot)
    )
    assert build.failed == 0 and read_store_marker(workdir)["procedures_ref"] == snapshot

    manifest = _manifest(store, routing_verticals=_verticals(store), procedure_snapshot=snapshot)
    pinned = PinnedRouting(store, lambda _: manifest, lightrag_opener(store, workdir, embedder, client), loop=loop)
    ref = _put(store, _claim_set().model_dump_json().encode())
    try:
        results = [_handle(store, ref, client, pinned, loop) for _ in range(2)]
    finally:
        pinned.close()
    for result in results:
        assert result.procedures_ref == snapshot and len(result.claims) == 2
        assert all(c.sections_seen for c in result.claims)  # LightRAG found sections for each claim
