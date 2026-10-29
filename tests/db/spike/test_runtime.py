"""LDR-0002 criteria run in-process: happy path, idempotent submit, dynamic tasks,
lane isolation and throughput."""
from __future__ import annotations

import threading
import time

import pytest
from dbos import DBOS
from dbos import error as dbos_error

from cfr_v2.contracts import AdmissionStatus, Lane
from cfr_v2.runtime import FINAL_STATE, STAGES, StageInput, StageOutput, submit
from cfr_v2.runtime.stages import placeholder_handler
from cfr_v2.state import CaseStateRepository

from .conftest import make_request, run_id, wait_for_state

pytestmark = pytest.mark.db


def test_case_reaches_plan_ready_with_one_commit_per_stage(running):
    record, handle = submit(make_request("happy-1"), release_manifest_ref="spike@0")
    assert record.status is AdmissionStatus.ACCEPTED
    assert handle.get_result() == FINAL_STATE
    with running.sessions() as s:
        repo = CaseStateRepository(s)
        assert repo.state_history(record.case_run_id) == ["ADMITTED", *(st.state for st in STAGES), FINAL_STATE]
        assert sorted(repo.committed_tasks(record.case_run_id)) == sorted(st.logical_task_id for st in STAGES)


def test_resubmitting_attaches_to_the_same_workflow(running):
    first, h1 = submit(make_request("dup-1"), release_manifest_ref="spike@0")
    second, h2 = submit(make_request("dup-1"), release_manifest_ref="spike@0")
    assert second.status is AdmissionStatus.DUPLICATE
    assert h1.get_workflow_id() == h2.get_workflow_id() == first.case_run_id
    assert h2.get_result() == FINAL_STATE
    with running.sessions() as s:
        assert CaseStateRepository(s).task_attempts(first.case_run_id, "c02.understanding") == ["COMMITTED"]


def test_stage_failure_records_failed_state(running):
    def broken(inp: StageInput) -> StageOutput:
        raise ValueError("boom")

    running.handlers = {**running.handlers, "c03.primary_selection": broken}
    record, handle = submit(make_request("fail-1"), release_manifest_ref="spike@0")
    with pytest.raises(dbos_error.DBOSMaxStepRetriesExceeded):
        handle.get_result()
    with running.sessions() as s:
        history = CaseStateRepository(s).state_history(record.case_run_id)
    assert history[-1] == "FAILED"
    assert "PLAN_READY" not in history


# -- dynamic DAG: tasks added while the workflow runs ------------------------------

_executed: list[str] = []


@DBOS.step()
def _spike_task(name: str) -> str:
    _executed.append(name)
    return name


@DBOS.workflow(name="spike_dynamic_plan")
def spike_dynamic_plan(initial: list[str]) -> list[str]:
    pending, done = list(initial), []
    while pending:
        done.append(_spike_task(pending.pop(0)))
        if not pending:
            # A plan revision arrives as a durable message, so replay is deterministic.
            revision = DBOS.recv("plan_revision", timeout_seconds=3)
            if revision:
                pending.extend(revision)
    return done


def test_tasks_added_mid_run_are_executed(running):
    _executed.clear()
    handle = DBOS.start_workflow(spike_dynamic_plan, ["retrieve.policy", "assess.rla"])
    DBOS.send(handle.get_workflow_id(), ["retrieve.lumi", "assess.rla.rev1"], topic="plan_revision")
    assert handle.get_result() == ["retrieve.policy", "assess.rla", "retrieve.lumi", "assess.rla.rev1"]
    assert _executed == ["retrieve.policy", "assess.rla", "retrieve.lumi", "assess.rla.rev1"]


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
        with rt.sessions() as s:
            repo = CaseStateRepository(s)
            finished_batch = [b for b in batch_ids if FINAL_STATE in repo.state_history(b)]
        assert finished_batch == []  # batch lane still saturated
        assert realtime_latency < 5.0
    finally:
        release.set()
        wait_for_state(settings.state_database_url, batch_ids, FINAL_STATE, timeout=60)
        stop_runtime()
