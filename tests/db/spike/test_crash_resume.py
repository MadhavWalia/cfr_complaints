"""LDR-0002 criterion (re-run for LDR-0011): kill a worker mid-step; another process
reclaims the job once its lease expires and finishes it with no duplicate commit."""
from __future__ import annotations

import signal

import pytest

from cfr_v2.runtime import FINAL_STATE, STAGES
from cfr_v2.state import CaseStateRepository, Database

from .conftest import run_id, spawn_worker, stop_worker, wait_for_file, wait_for_state, worker_env

pytestmark = [pytest.mark.db, pytest.mark.slow]


def test_killed_worker_resumes_without_duplicate_commits(tmp_path, runtime_settings, s3_settings, store):
    case = "crash-1"
    rid = run_id(case)
    marker = tmp_path / "blocked"
    base_env = worker_env(runtime_settings, s3_settings, CFR_SPIKE_CASE=case)

    # 1. Submit and hang inside the routing stage, then kill -9.
    first = spawn_worker(
        base_env | {"CFR_SPIKE_MODE": "submit", "CFR_SPIKE_BLOCK_TASK": "c04.routing", "CFR_SPIKE_MARKER": str(marker)}
    )
    wait_for_file(marker, first, timeout=60)
    first.send_signal(signal.SIGKILL)
    first.wait(timeout=10)

    db = Database(runtime_settings.state_database_url, max_connections=2)
    with db.transaction() as s:
        repo = CaseStateRepository(s)
        assert repo.state_history(rid) == ["ADMITTED", "UNDERSTANDING", "SELECTING", "ROUTING"]
        assert repo.committed_tasks(rid) == ["c02.understanding", "c03.primary_selection"]

    # 2. A different worker process reclaims the job after the dead one's lease expires.
    second = spawn_worker(base_env | {"CFR_SPIKE_MODE": "serve", "CFR_SPIKE_EXECUTOR_ID": "recovery-worker"})
    try:
        wait_for_state(runtime_settings.state_database_url, [rid], FINAL_STATE, timeout=60)
    finally:
        stop_worker(second)

    with db.transaction() as s:
        repo = CaseStateRepository(s)
        assert repo.state_history(rid) == ["ADMITTED", *(st.state for st in STAGES), FINAL_STATE]
        assert s.one("SELECT status, attempts FROM workflow_job WHERE case_run_id = %(r)s", {"r": rid}) == ("SUCCEEDED", 2)
        assert sorted(repo.committed_tasks(rid)) == sorted(st.logical_task_id for st in STAGES)
        # Completed stages were replayed from checkpoints, not re-committed.
        for st in STAGES:
            assert repo.task_attempts(rid, st.logical_task_id) == ["COMMITTED"], st.logical_task_id
    db.close()
