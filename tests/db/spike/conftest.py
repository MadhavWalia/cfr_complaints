"""Fixtures for the DBOS validation spike (LDR-0002 exit criteria).

Needs both docker-compose services: Postgres and RustFS (S3-compatible, LDR-0009).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
import sqlalchemy as sa
from botocore.exceptions import BotoCoreError, ClientError

from cfr_v2.artifacts import S3ArtifactStore, S3Credentials, S3Settings
from cfr_v2.contracts import AdmissionKey, IngestionRequest, Lane
from cfr_v2.runtime import RuntimeSettings, start_runtime, stop_runtime
from cfr_v2.state import case_run_id_for, make_engine

from ..conftest import _url_for

S3_ENDPOINT = os.environ.get("CFR_TEST_S3_ENDPOINT", "http://localhost:9000")
S3_KEY = os.environ.get("CFR_TEST_S3_ACCESSKEY", "cfr-dev")
S3_SECRET = os.environ.get("CFR_TEST_S3_SECRET", "cfr-dev-secret")


@pytest.fixture(scope="session")
def s3_available():
    client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        region_name="us-east-1",
    )
    try:
        client.list_buckets()
    except (BotoCoreError, ClientError) as exc:
        pytest.skip(f"S3 store not reachable at {S3_ENDPOINT} ({exc.__class__.__name__}); run `docker compose up -d`")


@pytest.fixture
def s3_settings(s3_available) -> S3Settings:
    suffix = uuid.uuid4().hex[:8]
    return S3Settings(
        endpoint_url=S3_ENDPOINT,
        region_name="us-east-1",
        credentials=S3Credentials(S3_KEY, S3_SECRET),
        restricted_credentials=None,
        ca_bundle=None,
        bucket=f"cfr-v2-test-{suffix}",
        restricted_bucket=f"cfr-v2-test-{suffix}-restricted",
    )


@pytest.fixture
def store(s3_settings) -> S3ArtifactStore:
    st = S3ArtifactStore(s3_settings)
    st.ensure_buckets()
    return st


@pytest.fixture
def dbos_system_db(admin_engine):
    name = f"cfr_test_dbos_{uuid.uuid4().hex[:10]}"
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    yield _url_for(name)
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def runtime_settings(fresh_db, dbos_system_db) -> RuntimeSettings:
    return RuntimeSettings(
        state_database_url=fresh_db[1],
        dbos_system_database_url=dbos_system_db,
        batch_queue_concurrency=50,
        realtime_queue_concurrency=10,
        queue_polling_interval_sec=0.1,
    )


@pytest.fixture
def running(runtime_settings, store):
    """Start the runtime in-process; handlers can be swapped via ``runtime.handlers``."""
    rt = start_runtime(runtime_settings, store)
    yield rt
    stop_runtime()


def make_request(case: str, *, lane: Lane = Lane.BATCH, rev: int = 0) -> IngestionRequest:
    return IngestionRequest(
        key=AdmissionKey(source_system="icare", source_case_id=case, ingestion_revision=rev),
        raw_transcript_ref=None,
        lane=lane,
        received_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
    )


def run_id(case: str, rev: int = 0) -> str:
    return case_run_id_for(AdmissionKey(source_system="icare", source_case_id=case, ingestion_revision=rev))


def wait_for_state(state_url: str, case_run_ids: list[str], state: str, *, timeout: float) -> None:
    """Wait until every run reached ``state``, with one aggregate query per tick.

    Kept deliberately light: polling each run individually from the worker
    process competes with the workflows for the same database round trips.
    """
    engine = make_engine(state_url, pool_size=1)
    query = sa.text(
        "SELECT count(DISTINCT case_run_id) FROM case_run_event WHERE state = :state AND case_run_id = ANY(:ids)"
    )
    deadline = time.monotonic() + timeout
    try:
        with engine.connect() as conn:
            while True:
                done = conn.execute(query, {"state": state, "ids": case_run_ids}).scalar_one()
                conn.commit()
                if done == len(case_run_ids):
                    return
                if time.monotonic() > deadline:
                    raise AssertionError(f"{len(case_run_ids) - done} of {len(case_run_ids)} runs did not reach {state}")
                time.sleep(0.25)
    finally:
        engine.dispose()


def db_elapsed_seconds(state_url: str, case_run_ids: list[str]) -> float:
    """First admission to last PLAN_READY, from database timestamps (one clock)."""
    engine = make_engine(state_url, pool_size=1)
    try:
        with engine.connect() as conn:
            return conn.execute(
                sa.text(
                    "SELECT EXTRACT(EPOCH FROM max(created_at) FILTER (WHERE state = 'PLAN_READY')"
                    " - min(created_at) FILTER (WHERE state = 'ADMITTED'))"
                    " FROM case_run_event WHERE case_run_id = ANY(:ids)"
                ),
                {"ids": case_run_ids},
            ).scalar_one()
    finally:
        engine.dispose()


# -- worker subprocesses -------------------------------------------------------------

WORKER = Path(__file__).with_name("worker_main.py")
REPO_ROOT = Path(__file__).resolve().parents[3]


def worker_env(runtime_settings: RuntimeSettings, s3_settings: S3Settings, **extra: str) -> dict[str, str]:
    return {
        "CFR_STATE_DATABASE_URL": runtime_settings.state_database_url,
        "CFR_DBOS_SYSTEM_DATABASE_URL": runtime_settings.dbos_system_database_url,
        "CFR_SPIKE_S3": json.dumps(
            {
                "endpoint_url": s3_settings.endpoint_url,
                "access_key": s3_settings.credentials.access_key,
                "secret_key": s3_settings.credentials.secret_key,
                "bucket": s3_settings.bucket,
                "restricted_bucket": s3_settings.restricted_bucket,
            }
        ),
        **extra,
    }


def spawn_worker(env_extra: dict[str, str]) -> subprocess.Popen:
    env = os.environ | env_extra | {"PYTHONPATH": str(REPO_ROOT / "src")}
    return subprocess.Popen(
        [sys.executable, str(WORKER)], env=env, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )


def wait_for_file(path: Path, proc: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if proc.poll() is not None:
            raise AssertionError(f"worker exited early:\n{proc.stdout.read().decode()}")
        if time.monotonic() > deadline:
            proc.kill()
            raise AssertionError(f"worker did not signal {path.name} in time:\n{proc.stdout.read().decode()}")
        time.sleep(0.05)


def stop_worker(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
