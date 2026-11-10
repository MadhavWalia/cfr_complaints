"""The ``c04.routing`` stage on the real runner (LDR-0021): a case reaches the final state with a
``RoutingResult`` routed with what its pinned manifest names; a release without routing pins fails.
Only the chat model and the procedure search are fake."""

from __future__ import annotations

import pytest

from cfr_v2.contracts import AuthorizationContext, Classification, ReleaseManifest, RoutingResult
from cfr_v2.llm import FakeModelClient
from cfr_v2.llm.dspy_lm import gateway_lm
from cfr_v2.registry import load_pinned, pin_manifest
from cfr_v2.routing.handler import PinnedRouting, make_routing_handler
from cfr_v2.runtime import FINAL_STATE, StageOutput, submit
from cfr_v2.state import CaseStateRepository
from tests.unit.routing.test_handler import Opener
from tests.unit.routing.test_router import CATALOG, DOCUMENT, SNAPSHOT, _router_reply
from tests.unit.selection.test_selector import _claim_set

from .spike.conftest import make_request, run_id, wait_for_state

pytestmark = pytest.mark.db

READER = AuthorizationContext(principal="test", purpose="check")


def _start(running, store, **pins) -> str:
    """Pass 2 gives a fixed claim set; routing is real. Returns the pinned manifest's ref."""
    claim_set = _claim_set()
    opener = Opener()
    routing = PinnedRouting(
        store, lambda ref: load_pinned(ref, store=store, db=running.db, authorization=READER), opener
    )
    lm = gateway_lm(FakeModelClient(responder=_router_reply), "fake-model")
    running.handlers = {
        **running.handlers,
        "c02.pass2": lambda inp: StageOutput(claim_set.model_dump_json().encode()),
        "c04.routing": make_routing_handler(store, lm, routing),
    }
    manifest = ReleaseManifest.build(release_id="cfr-v2-routing-test", **pins)
    return pin_manifest(manifest, store=store, db=running.db)


def test_a_case_is_routed_with_its_pinned_document_and_snapshot(running, store):
    verticals = store.put(DOCUMENT.encode(), media_type="text/markdown", classification=Classification.INTERNAL)
    manifest_ref = _start(running, store, routing_verticals=verticals.metadata.ref, procedure_snapshot=SNAPSHOT)
    submit(make_request("route-ok"), release_manifest_ref=manifest_ref)
    wait_for_state(running.settings.state_database_url, [run_id("route-ok")], FINAL_STATE, timeout=60)

    with running.db.transaction() as tx:
        [done] = [
            c
            for c in CaseStateRepository(tx).committed_results(run_id("route-ok"))
            if c.logical_task_id == "c04.routing"
        ]
    result = RoutingResult.model_validate_json(
        store.get(done.output_artifact_refs[0], classification=Classification.INTERNAL, authorization=READER)
    )
    assert done.program_version == "route-1"
    assert (result.claim_set_hash, result.procedures_ref, result.verticals_sha256) == (
        _claim_set().claim_set_hash,
        SNAPSHOT,
        CATALOG.sha256,
    )
    assert [c.candidates[0].vertical_id for c in result.claims] == ["HOLD", "HOLD"]


def test_a_release_without_routing_pins_fails_the_run(running, store):
    manifest_ref = _start(running, store)
    submit(make_request("route-unpinned"), release_manifest_ref=manifest_ref)
    wait_for_state(running.settings.state_database_url, [run_id("route-unpinned")], "FAILED", timeout=60)
