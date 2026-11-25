"""Retiring and restoring runs on a real database (LDR-0045 step 3): a retired run leaves the
Cases list, the exports and the counts, comes back on restore, and every change is a new row."""

from __future__ import annotations

import pytest

from cfr_v2.console import cases, status
from cfr_v2.contracts import ReleaseManifest
from cfr_v2.evaluation import source
from cfr_v2.registry import pin_manifest
from cfr_v2.runtime import FINAL_STATE, submit
from cfr_v2.runtime.retire import apply

from .spike.conftest import make_request, run_id, wait_for_state

pytestmark = pytest.mark.db


def _listed(db, retired=None):
    page = cases.list_cases(db, release="cfr-v2-retire", filters=cases.CaseFilters(retired=retired))
    return {r.case_run_id: r.retired for r in page.rows}


def test_a_retired_run_is_hidden_everywhere_until_restored(running, store):
    ref = pin_manifest(ReleaseManifest.build(release_id="cfr-v2-retire"), store=store, db=running.db)
    for case in ("RT-1", "RT-2"):
        submit(make_request(case), release_manifest_ref=ref)
    one, two = run_id("RT-1"), run_id("RT-2")
    wait_for_state(running.settings.state_database_url, [one, two], FINAL_STATE, timeout=60)

    with running.db.transaction() as tx:
        assert apply(tx, "retire", [one], "test runs")["changed"] == [one]
    with running.db.transaction() as tx:
        assert apply(tx, "retire", [one], None) == {"changed": [], "already retired": [one]}
        assert tx.one("SELECT reason FROM case_run_retired WHERE case_run_id = %(r)s", {"r": one}) == ("test runs",)

    assert _listed(running.db) == {two: False}
    assert _listed(running.db, "show") == {one: True, two: False}
    assert _listed(running.db, "only") == {one: True}
    assert cases.matching_runs(running.db, release="cfr-v2-retire", filters=cases.CaseFilters()).ids == (two,)
    with running.db.transaction() as tx:
        latest = tx.column(
            source.LATEST_RUNS_SQL,
            {"release": "cfr-v2-retire", "task": "c02.pass1", "start": None, "end": None, "cases": None,
             "seed": None, "limit": None},
        )  # fmt: skip
        counts = dict(tx.all(status.STAGE_SQL, {"release": "cfr-v2-retire"}))
        pinned = [r for r in tx.all(cases.RELEASES_SQL) if r[0] == "cfr-v2-retire"]
    assert latest == [two] and counts == {FINAL_STATE: 1}
    assert pinned[0][3:] == (2, 1)  # two runs, one retired

    with running.db.transaction() as tx:
        assert apply(tx, "restore", [one, two], None) == {"changed": [one], "not retired": [two]}
    assert _listed(running.db) == {one: False, two: False}
    with running.db.transaction() as tx:
        rows = tx.all("SELECT retired FROM case_run_retirement WHERE case_run_id = %(r)s ORDER BY event_id", {"r": one})
    assert rows == [(True,), (False,)]  # both changes on record
    with pytest.raises(Exception, match="append-only"), running.db.transaction() as tx:
        tx.execute("UPDATE case_run_retirement SET reason = 'x'")  # a change is a new row, never an edit
