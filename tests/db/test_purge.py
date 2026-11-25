"""Purging retired runs on a real database (LDR-0045 step 4): only retired runs, never a source
whose re-runs stay, every row of theirs deleted, a log kept, and a plain DELETE still refused."""

from __future__ import annotations

import pytest

from cfr_v2.contracts import AdmissionStatus, ReleaseManifest
from cfr_v2.registry import pin_manifest
from cfr_v2.runtime import FINAL_STATE, submit
from cfr_v2.runtime.rerun import create, plan, read, stage_for
from cfr_v2.runtime.retire import apply

from .spike.conftest import make_request, run_id, wait_for_state

pytestmark = pytest.mark.db

TABLES = ("task_attempt", "task_execution", "step_checkpoint", "case_run_event", "workflow_job",
          "case_run_retirement", "admission", "case_run")  # fmt: skip


def _rows(db, run):
    with db.transaction() as tx:
        return {t: tx.scalar(f"SELECT count(*) FROM {t} WHERE case_run_id = %(r)s", {"r": run}) for t in TABLES}


def test_retired_runs_and_their_re_runs_are_purged_together_and_logged(running, store):
    ref = pin_manifest(ReleaseManifest.build(release_id="cfr-v2-purge"), store=store, db=running.db)
    other = pin_manifest(ReleaseManifest.build(release_id="cfr-v2-purge-2"), store=store, db=running.db)
    submit(make_request("PG-1"), release_manifest_ref=ref)
    source = run_id("PG-1")
    wait_for_state(running.settings.state_database_url, [source], FINAL_STATE, timeout=60)
    start, stage = stage_for("selection")
    found = read(running.db, ids=[source], release=None, cases=None, sample=None, seed=None, manifest_ref=other,
                 stage_id=stage.logical_task_id)  # fmt: skip
    [item] = plan(*found, start=start, manifest_ref=other, include_failed=False)
    with running.db.transaction() as tx:
        create(tx, item, stage=stage, manifest_ref=other, max_attempts=3)
    if running.runner is not None:
        from cfr_v2.contracts import Lane

        running.runner.wake(Lane.BATCH)
    wait_for_state(running.settings.state_database_url, [item.rerun_id], FINAL_STATE, timeout=60)

    with running.db.transaction() as tx:  # not retired: nothing happens
        assert apply(tx, "purge", [source], None)["not retired: retire it first"] == [source]
        apply(tx, "retire", [source], "old")
    with running.db.transaction() as tx:  # its re-run would be left pointing at nothing
        decided = apply(tx, "purge", [source], None)
    assert decided["has re-runs that aren't selected: purge them together"] == [source]
    with running.db.transaction() as tx:
        apply(tx, "retire", [item.rerun_id], "old")
    with running.db.transaction() as tx:
        assert apply(tx, "purge", [source, item.rerun_id], "done with them")["changed"] == [source, item.rerun_id]

    for run in (source, item.rerun_id):
        assert set(_rows(running.db, run).values()) == {0}
    with running.db.transaction() as tx:
        log = tx.all(
            "SELECT case_run_id, source_case_id, rerun_of, reason FROM case_run_purge ORDER BY rerun_of NULLS FIRST"
        )
        assert tx.scalar("SELECT count(*) FROM \"case\" WHERE source_case_id = 'PG-1'") == 1  # the case stays
    assert log == [(source, "PG-1", None, "done with them"), (item.rerun_id, "PG-1", source, "done with them")]

    record, _ = submit(make_request("PG-1"), release_manifest_ref=ref)  # its admitted run is gone: admitted again
    assert record.status is AdmissionStatus.ACCEPTED


def test_a_plain_delete_is_still_refused(running, store):
    ref = pin_manifest(ReleaseManifest.build(release_id="cfr-v2-purge-3"), store=store, db=running.db)
    submit(make_request("PG-2"), release_manifest_ref=ref)
    with pytest.raises(Exception, match="append-only"), running.db.transaction() as tx:
        tx.execute("DELETE FROM case_run_event WHERE case_run_id = %(r)s", {"r": run_id("PG-2")})
    with pytest.raises(Exception, match="only retired runs can be purged"), running.db.transaction() as tx:
        tx.scalar("SELECT cfr_purge_case_runs(%(r)s::text[], NULL)", {"r": [run_id("PG-2")]})
