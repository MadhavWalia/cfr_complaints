"""Release manifests pinned at admission (P4, Phase B).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DDL = r"""
CREATE TABLE release_manifest (
    manifest_ref TEXT NOT NULL,
    release_id TEXT NOT NULL,
    checksum TEXT NOT NULL,
    artifact_ref TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_release_manifest PRIMARY KEY (manifest_ref),
    CONSTRAINT ck_release_manifest_checksum_hex CHECK (checksum ~ '^[0-9a-f]{64}$')
);

CREATE TRIGGER "release_manifest_append_only" BEFORE UPDATE OR DELETE ON release_manifest
    FOR EACH ROW EXECUTE FUNCTION cfr_reject_mutation();
CREATE TRIGGER "release_manifest_no_truncate" BEFORE TRUNCATE ON release_manifest
    FOR EACH STATEMENT EXECUTE FUNCTION cfr_reject_mutation();
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute("DROP TABLE release_manifest")
