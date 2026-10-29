"""LDR-0002 criteria re-run on the in-house runner (LDR-0011): happy path, idempotent
submit, failure handling, checkpoints, retries and lane isolation."""
from __future__ import annotations

import threading
import time

import pytest

from cfr_v2.contracts import AdmissionStatus, Lane
from cfr_v2.runtime import FINAL_STATE, STAGES, StageInput, StageOutput, WorkflowFailedError, submit
from cfr_v2.runtime.stages import placeholder_handler
from cfr_v2.state import CaseStateRepository

from .conftest import make_request, run_id, wait_for_state

pytestmark = pytest.mark.db


def _job(rt, rid):
    with rt.db.transaction() as tx:
        return tx.one("SELECT status, attempts, last_error FROM workflow_job WHERE case_run_id = %(r)s", {"r": rid})


def test_case_reaches_plan_ready_with_one_commit_per_stage(running):
    record, handle = submit(make_request("happy-1"), release_manifest_ref="spike@0")
    assert record.status is AdmissionStatus.ACCEPTED
    assert handle.get_result() == FINAL_STATE
    with running.db.transaction() as s:
        repo = CaseStateRepository(s)
        assert repo.state_history(record.case_run_id) == ["ADMITTED", *(st.state for st in STAGES), FINAL_STATE]
        assert sorted(repo.committed_tasks(record.case_run_id)) == sorted(st.logical_task_id for st in STAGES)
    assert _job(running, record.case_run_id) == ("SUCCEEDED", 1, None)


def test_resubmitting_attaches_to_the_same_run(running):
    first, h1 = submit(make_request("dup-1"), release_manifest_ref="spike@0")
    second, h2 = submit(make_request("dup-1"), release_manifest_ref="spike@0")
    assert second.status is AdmissionStatus.DUPLICATE
    assert h1.get_workflow_id() == h2.get_workflow_id() == first.case_run_id
    assert h2.get_result() == FINAL_STATE
    with running.db.transaction() as s:
        assert CaseStateRepository(s).task_attempts(first.case_run_id, "c02.understanding") == ["COMMITTED"]
        assert s.scalar("SELECT count(*) FROM workflow_job") == 1


def test_stage_failure_records_failed_state(running):
    calls = []

    def broken(inp: StageInput) -> StageOutput:
        calls.append(inp.case_run_id)
        raise ValueError("boom")

    running.handlers = {**running.handlers, "c03.primary_selection": broken}
    record, handle = submit(make_request("fail-1"), release_manifest_ref="spike@0")
    with pytest.raises(WorkflowFailedError, match="ValueError"):
        handle.get_result()
    with running.db.transaction() as s:
        history = CaseStateRepository(s).state_history(record.case_run_id)
    assert history[-1] == "FAILED"
    assert "PLAN_READY" not in history
    assert len(calls) == running.settings.step_max_attempts  # retried as a step, not as a whole run
    assert _job(running, record.case_run_id)[0] == "FAILED"


def test_commit_failure_retries_the_run_and_reuses_the_produce_checkpoint(running, monkeypatch):
    from cfr_v2.runtime import workflow

    produced: list[str] = []
    real_commit = workflow._commit
    failures = {"left": 1}

    def counting(inp: StageInput) -> StageOutput:
        produced.append(inp.stage.logical_task_id)
        return placeholder_handler(inp)

    def flaky_commit(repo, run, stage, *args):
        if stage.logical_task_id == "c04.routing" and failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("database went away")  # infrastructure error after produce
        return real_commit(repo, run, stage, *args)

    running.handlers = {st.logical_task_id: counting for st in STAGES}
    monkeypatch.setattr(workflow, "_commit", flaky_commit)
    record, handle = submit(make_request("retry-1"), release_manifest_ref="spike@0")
    assert handle.get_result() == FINAL_STATE

    assert produced == [st.logical_task_id for st in STAGES]  # c04 was produced once, despite the retry
    assert _job(running, record.case_run_id) == ("SUCCEEDED", 2, "ConnectionError")
    with running.db.transaction() as s:
        repo = CaseStateRepository(s)
        for st in STAGES:
            assert repo.task_attempts(record.case_run_id, st.logical_task_id) == ["COMMITTED"]


def test_run_fails_after_max_attempts(running, monkeypatch):
    from cfr_v2.runtime import workflow

    def always_down(*args):
        raise ConnectionError("database went away")

    monkeypatch.setattr(workflow, "_commit", always_down)
    record, handle = submit(make_request("down-1"), release_manifest_ref="spike@0")
    with pytest.raises(WorkflowFailedError, match="ConnectionError"):
        handle.get_result(timeout=30)
    assert _job(running, record.case_run_id)[:2] == ("FAILED", running.settings.max_job_attempts)


def test_unfinished_job_is_released_at_shutdown(runtime_settings, store):
    from cfr_v2.runtime import start_runtime, stop_runtime

    started, release = threading.Event(), threading.Event()

    def slow(inp: StageInput) -> StageOutput:
        started.set()
        release.wait(timeout=10)
        return placeholder_handler(inp)

    rt = start_runtime(runtime_settings, store, handlers={st.logical_task_id: slow for st in STAGES})
    try:
        record, _ = submit(make_request("stop-1"), release_manifest_ref="spike@0")
        assert started.wait(timeout=10)
        db = rt.db
        with db.transaction() as tx:
            assert tx.scalar("SELECT lease_owner FROM workflow_job WHERE case_run_id = %(r)s", {"r": record.case_run_id})
        rt.runner.stop(timeout=0)
        with db.transaction() as tx:
            row = tx.one("SELECT status, lease_owner FROM workflow_job WHERE case_run_id = %(r)s", {"r": record.case_run_id})
        assert row == ("QUEUED", None)  # immediately claimable by another process
    finally:
        release.set()
        rt.runner = None
        stop_runtime()


# -- lane isolation --------------------------------------------------------------


def test_saturated_batch_lane_does_not_block_realtime(runtime_settings, store):
    from cfr_v2.runtime import start_runtime, stop_runtime

    settings = runtime_settings.__class__(**{**runtime_settings.__dict__, "batch_queue_concurrency": 2})
    release = threading.Event()

    def slow_for_batch(inp: StageInput) -> StageOutput:
        if inp.case_run_id != run_id("rt-1"):
            release.wait(timeout=30)
        return placeholder_handler(inp)

    handlers = {st.logical_task_id: placeholder_handler for st in STAGES}
    handlers["c02.understanding"] = slow_for_batch
    rt = start_runtime(settings, store, handlers=handlers)
    try:
        batch_ids = [submit(make_request(f"batch-{i}"), release_manifest_ref="spike@0")[0].case_run_id for i in range(8)]
        time.sleep(0.5)  # let the two batch slots fill up
        start = time.monotonic()
        rt_record, rt_handle = submit(make_request("rt-1", lane=Lane.REALTIME), release_manifest_ref="spike@0")
        assert rt_handle.get_result() == FINAL_STATE
        realtime_latency = time.monotonic() - start
        with rt.db.transaction() as s:
            repo = CaseStateRepository(s)
            finished_batch = [b for b in batch_ids if FINAL_STATE in repo.state_history(b)]
            running_batch = s.scalar("SELECT count(*) FROM workflow_job WHERE lane = 'batch' AND status = 'RUNNING'")
        assert finished_batch == []  # batch lane still saturated
        assert running_batch == 2  # and capped at its concurrency
        assert realtime_latency < 5.0
    finally:
        release.set()
        wait_for_state(settings.state_database_url, batch_ids, FINAL_STATE, timeout=60)
        stop_runtime()
