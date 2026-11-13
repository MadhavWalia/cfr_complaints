"""``python -m cfr_v2.runtime``: a worker process runs jobs queued by another
process and drains cleanly on SIGTERM."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

from cfr_v2.intake.settings import IntakeSettings
from cfr_v2.registry import ReleaseRegistry, pin_manifest
from cfr_v2.runtime import FINAL_STATE, start_runtime, stop_runtime, submit
from cfr_v2.runtime.__main__ import main
from cfr_v2.state import Database

from .conftest import REPO_ROOT, make_request, run_id, wait_for_state

pytestmark = pytest.mark.db


def test_worker_cli_runs_queued_jobs_and_stops_on_sigterm(runtime_settings, s3_settings, store):
    rt = start_runtime(runtime_settings, store, listen_lanes=())  # submit-only, like intake
    try:
        # The real worker runs C02, whose Pass 2 reads the run's pinned manifest; pin one as intake does.
        # It must match the code's stages, or the worker won't start (LDR-0022). The case has no
        # transcript, so it has no claims and routing needs no LightRAG store.
        manifest = ReleaseRegistry(REPO_ROOT / "registry").load(IntakeSettings().release_id)
        submit(make_request("cli-1"), release_manifest_ref=pin_manifest(manifest, store=store, db=rt.db))
    finally:
        stop_runtime()

    proc = _worker(runtime_settings, s3_settings)
    try:
        wait_for_state(runtime_settings.state_database_url, [run_id("cli-1")], FINAL_STATE, timeout=60)
    finally:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0, out.decode()


def test_worker_cli_refuses_to_start_on_runs_of_an_older_release(runtime_settings, s3_settings, store):
    """Queued runs pinned to a release whose stage versions differ from the code's are never run:
    on 2026-11-10 thousands went through C02 and C03, then failed at routing (LDR-0022)."""
    rt = start_runtime(runtime_settings, store, listen_lanes=())
    try:
        manifest = ReleaseRegistry(REPO_ROOT / "registry").load("cfr-v2-phase-d")
        submit(make_request("cli-old"), release_manifest_ref=pin_manifest(manifest, store=store, db=rt.db))
    finally:
        stop_runtime()

    proc = _worker(runtime_settings, s3_settings)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 2, out.decode()
    text = out.decode()
    # Only digit-free parts: with ee-logging's redaction on, digits in a log line may be masked
    # ("cfr-v***-phase-d", "c***.routing"), so the full release id and task id can't be matched.
    for part in ("-phase-d", ".routing", "doesn't match this code", "not starting"):
        assert part in text, text
    db = Database(runtime_settings.state_database_url, max_connections=1)
    try:
        with db.transaction() as tx:
            status = tx.scalar("SELECT status FROM workflow_job WHERE case_run_id = %(r)s", {"r": run_id("cli-old")})
    finally:
        db.close()
    assert status == "QUEUED"  # untouched


def _worker(runtime_settings, s3_settings) -> subprocess.Popen:
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
    return subprocess.Popen(
        [sys.executable, "-m", "cfr_v2.runtime", "--lanes", "batch", "--drain-timeout", "5"],
        env=env,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


@pytest.mark.parametrize("lanes", ["bogus", ","])
def test_worker_cli_rejects_bad_lanes(lanes):
    with pytest.raises(SystemExit) as exc:
        main(["--lanes", lanes])
    assert exc.value.code == 2
