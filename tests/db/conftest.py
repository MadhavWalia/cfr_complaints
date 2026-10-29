"""Fixtures for tests that need the docker-compose Postgres.

A migrated template database is built once per session; each test gets a fresh
clone (``CREATE DATABASE ... TEMPLATE``) because P2 tables are append-only and
cannot be truncated between tests.
"""
from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from cfr_v2.state import make_engine, make_sessionmaker, run_migrations

ADMIN_URL = os.environ.get(
    "CFR_TEST_ADMIN_DATABASE_URL", "postgresql+psycopg://cfr:cfr@localhost:5432/postgres"
)


def _admin_engine() -> sa.Engine:
    return sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", pool_pre_ping=True)


def _url_for(db_name: str) -> str:
    return make_url(ADMIN_URL).set(database=db_name).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def admin_engine():
    engine = _admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable at {ADMIN_URL} ({exc.__class__.__name__}); run `docker compose up -d`")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def template_db(admin_engine):
    name = f"cfr_test_tmpl_{uuid.uuid4().hex[:8]}"
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    run_migrations(_url_for(name))
    yield name
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def fresh_db(admin_engine, template_db):
    """Name and URL of a new database cloned from the migrated template."""
    name = f"cfr_test_{uuid.uuid4().hex[:10]}"
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"'))
    yield name, _url_for(name)
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def state_engine(fresh_db):
    engine = make_engine(fresh_db[1])
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(state_engine):
    return make_sessionmaker(state_engine)
