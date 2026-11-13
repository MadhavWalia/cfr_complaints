"""Cancelling a release's queued runs (LDR-0029), without a database: what is asked of it."""

from __future__ import annotations

import json

import pytest

from cfr_v2.runtime import cancel


class FakeTx:
    def __init__(self, picked: list[str]):
        self.picked, self.calls = picked, []

    def all(self, sql, params):
        self.calls.append(("all", sql, params))
        return [(run,) for run in self.picked]

    def execute(self, sql, params):
        self.calls.append(("execute", sql, params))
        return len(params["ids"])


def test_only_queued_runs_of_that_release_and_never_a_delete():
    sql = cancel.CANCEL_SQL
    assert "j.status = 'QUEUED'" in sql and "split_part(c.release_manifest_ref, '@', 1) = %(release)s" in sql
    assert "SKIP LOCKED" in sql  # a run a worker is claiming right now is left to it
    assert "status = 'FAILED', last_error = 'CANCELLED'" in sql
    for text in (cancel.CANCEL_SQL, cancel.EVENT_SQL, cancel.COUNT_SQL):
        assert "DELETE" not in text.upper() and "TRUNCATE" not in text.upper()
    assert "ON CONFLICT (case_run_id, state) DO NOTHING" in cancel.EVENT_SQL


def test_cancelled_runs_record_the_state_with_the_release():
    tx = FakeTx(["run-b", "run-a"])
    assert cancel.cancel_queued(tx, "cfr-v2-phase-d") == ["run-a", "run-b"]
    (_, _, picked), (kind, sql, params) = tx.calls
    assert picked == {"release": "cfr-v2-phase-d"}
    assert kind == "execute" and "'CANCELLED'" in sql and params["ids"] == ["run-b", "run-a"]
    assert json.loads(params["detail"]) == {"reason": "cancelled while queued", "release": "cfr-v2-phase-d"}


def test_nothing_queued_records_nothing():
    tx = FakeTx([])
    assert cancel.cancel_queued(tx, "cfr-v2-phase-d") == []
    assert [c[0] for c in tx.calls] == ["all"]


@pytest.mark.parametrize("release", ["", "Phase D", "cfr-v2-phase-d@a220ea87891f", "x"])
def test_the_cli_takes_a_release_id_only(release):
    with pytest.raises(SystemExit) as exit_:
        cancel.main(["--release", release])
    assert exit_.value.code == 2
