from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from cfr_v2.state.schema import metadata

pytestmark = pytest.mark.db


def test_schema_py_matches_migrated_database(state_engine):
    """Frozen migration DDL and schema.py must describe the same tables."""
    with state_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = [d for d in compare_metadata(ctx, metadata) if not (d[0] == "remove_table" and d[1].name == "alembic_version")]
    assert diff == []


def test_head_revision_applied(state_engine):
    with state_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == "0001"
