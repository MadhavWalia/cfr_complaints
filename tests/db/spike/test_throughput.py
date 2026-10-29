"""LDR-0002 throughput criterion (clarified 2026-10-29).

One Python process caps at roughly 1,400 Postgres round trips/s however many
threads it runs (GIL + driver overhead), so throughput scales by adding worker
processes, not threads. The criterion is therefore:

* deployment-wide: >= 5 cases/s with several worker processes sharing the queues;
* per process: a floor that guards against regressions in round trips per case.

Elapsed time is measured with database timestamps: first admission to last PLAN_READY.
"""
from __future__ import annotations

import time

import pytest

from cfr_v2.contracts import Lane
from cfr_v2.runtime import FINAL_STATE, start_runtime, stop_runtime, submit

from .conftest import (
    db_elapsed_seconds,
    make_request,
    spawn_worker,
    stop_worker,
    wait_for_file,
    wait_for_state,
    worker_env,
)

pytestmark = [pytest.mark.db, pytest.mark.slow]

DEPLOYMENT_TARGET = 5.0  # cases/s, LDR-0002
PER_PROCESS_FLOOR = 3.0  # cases/s, regression guard for one worker process
WORKERS = 4
PER_WORKER_CONCURRENCY = 25


def _report(label: str, n: int, elapsed: float) -> float:
    rate = n / elapsed
    print(f"\n{label}: {n} cases in {elapsed:.1f}s = {rate:.1f} cases/s")
    return rate


def test_single_process_floor(running):
    n = 150
    ids = [submit(make_request(f"sp-{i}"), release_manifest_ref="spike@0")[0].case_run_id for i in range(n)]
    wait_for_state(running.settings.state_database_url, ids, FINAL_STATE, timeout=180)
    rate = _report("single process", n, db_elapsed_seconds(running.settings.state_database_url, ids))
    assert rate >= PER_PROCESS_FLOOR


def test_deployment_throughput_with_worker_processes(tmp_path, runtime_settings, s3_settings, store):
    # The test process only admits and enqueues; it executes no workflows.
    start_runtime(runtime_settings, store, listen_lanes=())
    workers = []
    try:
        for w in range(WORKERS):
            ready = tmp_path / f"ready-{w}"
            proc = spawn_worker(
                worker_env(
                    runtime_settings,
                    s3_settings,
                    CFR_SPIKE_MODE="serve",
                    CFR_SPIKE_EXECUTOR_ID=f"worker-{w}",
                    CFR_SPIKE_BATCH_CONCURRENCY=str(PER_WORKER_CONCURRENCY),
                    CFR_SPIKE_READY_MARKER=str(ready),
                )
            )
            workers.append(proc)
            wait_for_file(ready, proc, timeout=60)

        n = 400
        submitted_at = time.monotonic()
        ids = [submit(make_request(f"mp-{i}", lane=Lane.BATCH), release_manifest_ref="spike@0")[0].case_run_id for i in range(n)]
        print(f"\nsubmit loop: {n / (time.monotonic() - submitted_at):.1f} cases/s")
        wait_for_state(runtime_settings.state_database_url, ids, FINAL_STATE, timeout=300)
        rate = _report(f"{WORKERS} worker processes", n, db_elapsed_seconds(runtime_settings.state_database_url, ids))
        assert rate >= DEPLOYMENT_TARGET
    finally:
        for proc in workers:
            stop_worker(proc)
        stop_runtime()
