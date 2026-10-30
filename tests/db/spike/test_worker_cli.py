"""``python -m cfr_v2.runtime``: a worker process runs jobs queued by another
process and drains cleanly on SIGTERM."""
from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

from cfr_v2.runtime import FINAL_STATE, start_runtime, stop_runtime, submit
from cfr_v2.runtime.__main__ import main

from .conftest import REPO_ROOT, make_request, run_id, wait_for_state

pytestmark = pytest.mark.db


def test_worker_cli_runs_queued_jobs_and_stops_on_sigterm(runtime_settings, s3_settings, store):
    start_runtime(runtime_settings, store, listen_lanes=())  # submit-only, like intake
    try:
        submit(make_request("cli-1"), release_manifest_ref="spike@0")
    finally:
        stop_runtime()

    env = os.environ | {
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "CFR_STATE_DATABASE_URL": runtime_settings.state_database_url,
        "CFR_QUEUE_POLLING_INTERVAL_SEC": "0.1",
        "S3_ENDPOINT": s3_settings.endpoint_url,
        "REGION_NAME": "us-east-1",
        "S3_ACCESSKEY": s3_settings.credentials.access_key,
        "S3_SECRET": s3_settings.credentials.secret_key,
        "CFR_ARTIFACT_BUCKET": s3_settings.bucket,
        "CFR_RESTRICTED_ARTIFACT_BUCKET": s3_settings.restricted_bucket,
    }
    env.pop("CFR_OTLP_ENDPOINT", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "cfr_v2.runtime", "--lanes", "batch", "--drain-timeout", "5"],
        env=env,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_for_state(runtime_settings.state_database_url, [run_id("cli-1")], FINAL_STATE, timeout=60)
    finally:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0, out.decode()


@pytest.mark.parametrize("lanes", ["bogus", ","])
def test_worker_cli_rejects_bad_lanes(lanes):
    with pytest.raises(SystemExit) as exc:
        main(["--lanes", lanes])
    assert exc.value.code == 2
