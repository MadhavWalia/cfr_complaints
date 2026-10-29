"""Fixtures for tests that need the docker-compose Postgres.

A migrated template database is built once per session; each test gets a fresh
clone (``CREATE DATABASE ... TEMPLATE``) because P2 tables are append-only and
cannot be truncated between tests. Runtime fixtures also need the RustFS
S3-compatible store (LDR-0009).

``CFR_DB_DRIVER=psycopg2`` runs the whole suite on psycopg2 instead of psycopg 3.
"""
from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError

from cfr_v2.artifacts import S3ArtifactStore, S3Credentials, S3Settings
from cfr_v2.runtime import RuntimeSettings, start_runtime, stop_runtime
from cfr_v2.state import Database, autocommit_connection, run_migrations

ADMIN_URL = os.environ.get("CFR_TEST_ADMIN_DATABASE_URL", "postgresql://cfr:cfr@localhost:5432/postgres")


def _url_for(db_name: str) -> str:
    parts = urlsplit(ADMIN_URL)
    return urlunsplit(parts._replace(path=f"/{db_name}"))


def admin_execute(sql: str) -> None:
    with autocommit_connection(ADMIN_URL) as tx:
        tx.execute(sql)


@pytest.fixture(scope="session")
def admin_available():
    try:
        admin_execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable at {ADMIN_URL} ({exc.__class__.__name__}); run `docker compose up -d`")


@pytest.fixture(scope="session")
def template_db(admin_available):
    name = f"cfr_test_tmpl_{uuid.uuid4().hex[:8]}"
    admin_execute(f'CREATE DATABASE "{name}"')
    run_migrations(_url_for(name))
    yield name
    admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def fresh_db(template_db):
    """Name and URL of a new database cloned from the migrated template."""
    name = f"cfr_test_{uuid.uuid4().hex[:10]}"
    admin_execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
    yield name, _url_for(name)
    admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db(fresh_db):
    database = Database(fresh_db[1], max_connections=12)
    yield database
    database.close()


# -- S3 store and runtime -------------------------------------------------------------

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
def runtime_settings(fresh_db) -> RuntimeSettings:
    return RuntimeSettings(
        state_database_url=fresh_db[1],
        executor_id="test-executor",
        batch_queue_concurrency=50,
        realtime_queue_concurrency=10,
        queue_polling_interval_sec=0.1,
        lease_sec=3.0,
        heartbeat_sec=1.0,
        retry_backoff_sec=0.1,
        step_backoff_sec=0.05,
    )


@pytest.fixture
def running(runtime_settings, store):
    """Start the runtime in-process; handlers can be swapped via ``runtime.handlers``."""
    rt = start_runtime(runtime_settings, store)
    yield rt
    stop_runtime()
