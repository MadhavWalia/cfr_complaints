"""Routing stored claim sets of failed runs (LDR-0022): results stored, ids-only lines, resume, failures."""

from __future__ import annotations

import asyncio
import json

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.contracts import AuthorizationContext, Classification, RoutingResult
from cfr_v2.llm import FakeModelClient
from cfr_v2.llm.dspy_lm import gateway_lm
from cfr_v2.routing.backfill import PendingRun, already_done, backfill, summary
from cfr_v2.routing.handler import RoutingContext
from tests.unit.routing.test_router import CATALOG, SNAPSHOT, Search, _hit, _router_reply
from tests.unit.selection.test_selector import _claim_set

READER = AuthorizationContext(principal="test", purpose="check")


def _put(store, claim_set) -> str:
    raw = claim_set.model_dump_json().encode()
    return store.put(raw, media_type="application/json", classification=Classification.INTERNAL).metadata.ref


def test_runs_are_routed_stored_and_written_without_claim_text(tmp_path):
    store, client = InMemoryArtifactStore(), FakeModelClient(responder=_router_reply)
    runs = [
        PendingRun("run-1", "GC-1", _put(store, _claim_set())),
        PendingRun("run-2", "GC-2", _put(store, _claim_set(claims=()))),
        PendingRun("run-3", "GC-3", "sha256:" + "f" * 64),  # missing: recorded, the rest go on
    ]
    rows, registered = [], []
    outcomes = asyncio.run(
        backfill(
            runs,
            store,
            RoutingContext(CATALOG, Search([_hit(1, 1, 1)]), SNAPSHOT),
            gateway_lm(client, "m"),
            concurrency=2,
            register=registered.append,
            write=rows.append,
        )
    )
    by_run = {r["case_run_id"]: r for r in rows}
    assert by_run["run-1"]["claims"] == 2 and by_run["run-1"]["decisions"] == {"HOLD": 2}
    assert by_run["run-2"]["claims"] == 0 and by_run["run-2"]["routing_ref"]  # nothing to route, still stored
    assert by_run["run-3"]["error"] and by_run["run-3"]["routing_ref"] is None
    assert len(client.requests) == 2 and len(registered) == 2
    result = RoutingResult.model_validate_json(
        store.get(by_run["run-1"]["routing_ref"], classification=Classification.INTERNAL, authorization=READER)
    )
    assert result.claim_set_hash == _claim_set().claim_set_hash and result.procedures_ref == SNAPSHOT
    assert "hotel" not in json.dumps(rows) and "agent" not in json.dumps(rows)  # ids and counts only
    assert summary(outcomes)[0] == "3 runs: 2 routed, 1 failed; 2 claims"


def test_a_backfill_resumes_after_the_runs_it_routed(tmp_path):
    out = tmp_path / "backfill.jsonl"
    assert already_done(out) == set()
    out.write_text(
        json.dumps({"case_run_id": "run-1", "routing_ref": "sha256:" + "a" * 64})
        + "\n"
        + json.dumps({"case_run_id": "run-2", "routing_ref": None, "error": "ModelTimeoutError"})
        + "\n"
    )
    assert already_done(out) == {"run-1"}  # a failed run is tried again
