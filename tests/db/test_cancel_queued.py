"""``python -m cfr_v2.runtime.cancel`` (LDR-0029): a release's queued runs leave the queue and
record CANCELLED; other releases' runs, and the history, stay as they were."""

from __future__ import annotations

import pytest

from cfr_v2.contracts import ReleaseManifest
from cfr_v2.registry import pin_manifest
from cfr_v2.runtime import start_runtime, stop_runtime, submit
from cfr_v2.runtime.cancel import cancel_queued, count_queued, main
from cfr_v2.state import Database

from .spike.conftest import make_request, run_id

pytestmark = pytest.mark.db


def _queue(runtime_settings, store) -> None:
    """Runs queued under two releases, no worker taking them."""
    rt = start_runtime(runtime_settings, store, listen_lanes=())  # submit-only, like intake
    try:
        old = pin_manifest(ReleaseManifest.build(release_id="cfr-v2-cancel-old"), store=store, db=rt.db)
        new = pin_manifest(ReleaseManifest.build(release_id="cfr-v2-cancel-new"), store=store, db=rt.db)
        for case in ("OLD-1", "OLD-2"):
            submit(make_request(case), release_manifest_ref=old)
        submit(make_request("NEW-1"), release_manifest_ref=new)
    finally:
        stop_runtime()


def test_cancelling_takes_one_releases_queued_runs_off_the_queue(runtime_settings, store, monkeypatch):
    _queue(runtime_settings, store)
    db = Database(runtime_settings.state_database_url, max_connections=1)
    try:
        with db.transaction() as tx:
            assert count_queued(tx, "cfr-v2-cancel-old") == 2
            assert cancel_queued(tx, "cfr-v2-cancel-old") == sorted([run_id("OLD-1"), run_id("OLD-2")])
        with db.transaction() as tx:
            jobs = dict(tx.all("SELECT case_run_id, status || '/' || coalesce(last_error, '') FROM workflow_job"))
            states = dict(
                tx.all(
                    "SELECT DISTINCT ON (case_run_id) case_run_id, state FROM case_run_event"
                    " ORDER BY case_run_id, event_id DESC"
                )
            )
            history = [
                s
                for (s,) in tx.all(
                    "SELECT state FROM case_run_event WHERE case_run_id = %(r)s ORDER BY event_id",
                    {"r": run_id("OLD-1")},
                )
            ]
            assert count_queued(tx, "cfr-v2-cancel-old") == 0
            assert cancel_queued(tx, "cfr-v2-cancel-old") == []  # again: nothing left to cancel
    finally:
        db.close()
    assert jobs[run_id("OLD-1")] == jobs[run_id("OLD-2")] == "FAILED/CANCELLED"
    assert states[run_id("OLD-1")] == "CANCELLED"
    assert history == ["ADMITTED", "CANCELLED"]  # appended: the admission stays
    assert jobs[run_id("NEW-1")] == "QUEUED/" and states[run_id("NEW-1")] == "ADMITTED"  # another release: untouched

    # the CLI: a dry run changes nothing
    monkeypatch.setenv("CFR_STATE_DATABASE_URL", runtime_settings.state_database_url)
    assert main(["--release", "cfr-v2-cancel-new", "--dry-run"]) == 0
    db = Database(runtime_settings.state_database_url, max_connections=1)
    try:
        with db.transaction() as tx:
            assert count_queued(tx, "cfr-v2-cancel-new") == 1
    finally:
        db.close()
