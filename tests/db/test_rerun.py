"""Re-runs from a stage on the real runner (LDR-0045): the new run reuses the stages before the
chosen one, runs the rest under its own release, and leaves the source as it was. The handlers are
the placeholders, which write the run's id into each result, so a stage that ran again shows it."""

from __future__ import annotations

import json

import pytest

from cfr_v2.contracts import AdmissionStatus, AuthorizationContext, Classification, Lane, ReleaseManifest
from cfr_v2.registry import pin_manifest
from cfr_v2.runtime import FINAL_STATE, submit
from cfr_v2.runtime.rerun import create, plan, read, stage_for, summary
from cfr_v2.state import CaseStateRepository

from .spike.conftest import make_request, run_id, wait_for_state

pytestmark = pytest.mark.db

READER = AuthorizationContext(principal="test", purpose="check")


def _results(db, run):
    with db.transaction() as tx:
        return {c.logical_task_id: c.output_artifact_refs[0] for c in CaseStateRepository(tx).committed_results(run)}


def test_a_run_re_run_from_selection_reuses_pass_1_and_2_and_runs_the_rest(running, store):
    old = pin_manifest(ReleaseManifest.build(release_id="cfr-v2-rerun-old"), store=store, db=running.db)
    new = pin_manifest(ReleaseManifest.build(release_id="cfr-v2-rerun-new"), store=store, db=running.db)
    submit(make_request("RR-1"), release_manifest_ref=old)
    source = run_id("RR-1")
    wait_for_state(running.settings.state_database_url, [source], FINAL_STATE, timeout=60)
    before = _results(running.db, source)

    start, stage = stage_for("selection")
    sources, results, existing = read(
        running.db, ids=[source], release=None, cases=None, sample=None, seed=None, manifest_ref=new,
        stage_id=stage.logical_task_id,
    )  # fmt: skip
    [item] = plan(sources, results, existing, start=start, manifest_ref=new, include_failed=False)
    assert item.status == "rerun", summary([item], stage, "cfr-v2-rerun-new")
    with running.db.transaction() as tx:
        assert create(tx, item, stage=stage, manifest_ref=new, max_attempts=3)
    with running.db.transaction() as tx:  # asked twice: one run
        assert not create(tx, item, stage=stage, manifest_ref=new, max_attempts=3)
    if running.runner is not None:  # the job was queued outside the runtime: tell its lane
        running.runner.wake(Lane(item.source.lane))
    wait_for_state(running.settings.state_database_url, [item.rerun_id], FINAL_STATE, timeout=60)

    after = _results(running.db, item.rerun_id)
    assert after["c02.pass1"] == before["c02.pass1"] and after["c02.pass2"] == before["c02.pass2"]
    for task in ("c03.primary_selection", "c04.routing", "c05.planning"):
        assert after[task] != before[task]
        body = json.loads(store.get(after[task], classification=Classification.INTERNAL, authorization=READER))
        assert body["case_run_id"] == item.rerun_id
    assert _results(running.db, source) == before  # the source is untouched
    with running.db.transaction() as tx:
        run = CaseStateRepository(tx).case_run(item.rerun_id)
        admitted = tx.scalar(
            "SELECT detail FROM case_run_event WHERE case_run_id = %(run)s AND state = 'ADMITTED'",
            {"run": item.rerun_id},
        )
    assert (run["rerun_of"], run["rerun_from"], run["release_manifest_ref"]) == (source, "c03.primary_selection", new)
    assert admitted == {"rerun_of": source, "from": "c03.primary_selection"}

    # once re-run, the same request is still a duplicate admission, as before
    record, _ = submit(make_request("RR-1"), release_manifest_ref=old)
    assert record.status is AdmissionStatus.DUPLICATE and record.case_run_id == source

    # the preview now counts it as already re-run
    sources, results, existing = read(
        running.db, ids=[source], release=None, cases=None, sample=None, seed=None, manifest_ref=new,
        stage_id=stage.logical_task_id,
    )  # fmt: skip
    [again] = plan(sources, results, existing, start=start, manifest_ref=new, include_failed=False)
    assert again.status == "exists"
