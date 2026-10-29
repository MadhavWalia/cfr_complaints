from __future__ import annotations

import uuid

import pytest

from cfr_v2.state import Database, applied_versions, available_migrations, load_driver, run_migrations

from .conftest import _url_for, admin_execute

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
    "artifact",
    "case",
    "case_run",
    "admission",
    "case_run_event",
    "task_execution",
    "task_attempt",
    "release_manifest",
    "workflow_job",
    "step_checkpoint",
    "schema_migration",
}
MUTABLE = {"workflow_job", "schema_migration"}  # scheduling and bookkeeping only (LDR-0011)


def test_all_migrations_applied(db):
    assert applied_versions(db) == [m.version for m in available_migrations()] == ["0001", "0002", "0003"]


def test_expected_tables_and_append_only_triggers(db):
    with db.transaction() as tx:
        tables = set(tx.column("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        guarded = set(
            tx.column(
                "SELECT DISTINCT event_object_table FROM information_schema.triggers"
                " WHERE action_statement LIKE '%%cfr_reject_mutation%%'"
            )
        )
    assert tables == EXPECTED_TABLES
    # TRUNCATE triggers are not listed in information_schema; UPDATE/DELETE ones are.
    assert guarded == EXPECTED_TABLES - MUTABLE


def test_rerun_is_a_no_op(db):
    assert run_migrations(db) == []


@pytest.mark.parametrize("driver", ["psycopg", "psycopg2"])
def test_fresh_database_on_each_driver(admin_available, driver):
    name = f"cfr_test_mig_{uuid.uuid4().hex[:8]}"
    admin_execute(f'CREATE DATABASE "{name}"')
    database = Database(_url_for(name), driver=load_driver(driver))
    try:
        assert run_migrations(database) == ["0001", "0002", "0003"]
    finally:
        database.close()
        admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_database_migrated_by_alembic_is_adopted(admin_available):
    name = f"cfr_test_mig_{uuid.uuid4().hex[:8]}"
    admin_execute(f'CREATE DATABASE "{name}"')
    database = Database(_url_for(name))
    try:
        migrations = {m.version: m for m in available_migrations()}
        with database.transaction() as tx:  # what an Alembic-managed Phase B database looks like
            tx.execute(migrations["0001"].sql)
            tx.execute(migrations["0002"].sql)
            tx.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
            tx.execute("INSERT INTO alembic_version VALUES ('0002')")
        assert run_migrations(database) == ["0003"]
        assert applied_versions(database) == ["0001", "0002", "0003"]
    finally:
        database.close()
        admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_suite_runs_on_the_selected_driver(db):
    import os

    assert db.driver.name == {"auto": "psycopg"}.get(os.environ.get("CFR_DB_DRIVER", "auto"), os.environ.get("CFR_DB_DRIVER"))
