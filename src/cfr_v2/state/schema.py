"""P2 logical schema. All tables are insert-only.

Later phases add claim-set, selection, routing and plan tables in new migrations.
See lld/modules/p2-case-state/README.md.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


case = sa.Table(
    "case",
    metadata,
    sa.Column("case_id", sa.Text, primary_key=True),
    sa.Column("source_system", sa.Text, nullable=False),
    sa.Column("source_case_id", sa.Text, nullable=False),
    _created_at(),
    sa.UniqueConstraint("source_system", "source_case_id"),
)

case_run = sa.Table(
    "case_run",
    metadata,
    sa.Column("case_run_id", sa.Text, primary_key=True),  # also the DBOS workflow id
    sa.Column("case_id", sa.Text, sa.ForeignKey("case.case_id"), nullable=False),
    sa.Column("ingestion_revision", sa.Integer, nullable=False),
    sa.Column("release_manifest_ref", sa.Text, nullable=False),
    sa.Column("lane", sa.Text, nullable=False),
    sa.Column("request_ref", sa.Text, nullable=False),  # IngestionRequest artifact
    _created_at(),
    sa.UniqueConstraint("case_id", "ingestion_revision"),
    sa.CheckConstraint("lane IN ('batch', 'realtime')", name="lane"),
)

admission = sa.Table(
    "admission",
    metadata,
    sa.Column("admission_id", sa.Text, primary_key=True),
    sa.Column("case_id", sa.Text, sa.ForeignKey("case.case_id"), nullable=False),
    sa.Column("ingestion_revision", sa.Integer, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("case_run_id", sa.Text, sa.ForeignKey("case_run.case_run_id"), nullable=True),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=True),
    _created_at(),
    sa.CheckConstraint("status IN ('ACCEPTED', 'DUPLICATE', 'REJECTED')", name="status"),
    sa.CheckConstraint("(status = 'REJECTED') = (case_run_id IS NULL)", name="run_matches_status"),
    sa.Index(
        "uq_admission_accepted_key",
        "case_id",
        "ingestion_revision",
        unique=True,
        postgresql_where=sa.text("status = 'ACCEPTED'"),
    ),
)

CASE_RUN_STATES = (
    "ADMITTED",
    "UNDERSTANDING",
    "SELECTING",
    "ROUTING",
    "PLANNING",
    "PLAN_READY",
    "FAILED",
    "CANCELLED",
)

case_run_event = sa.Table(
    "case_run_event",
    metadata,
    sa.Column("event_id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("case_run_id", sa.Text, sa.ForeignKey("case_run.case_run_id"), nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    _created_at(),
    sa.CheckConstraint(f"state IN {CASE_RUN_STATES!r}", name="state"),
    sa.Index(None, "case_run_id", "event_id"),
    # A run passes through each state at most once (HD-04 coarse states).
    sa.UniqueConstraint("case_run_id", "state"),
)

artifact = sa.Table(
    "artifact",
    metadata,
    sa.Column("sha256", sa.Text, primary_key=True),
    sa.Column("bucket", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("media_type", sa.Text, nullable=False),
    sa.Column("classification", sa.Text, nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    _created_at(),
    sa.CheckConstraint("classification IN ('RESTRICTED', 'INTERNAL')", name="classification"),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
)

task_execution = sa.Table(
    "task_execution",
    metadata,
    sa.Column("task_execution_id", sa.Text, primary_key=True),
    sa.Column("case_run_id", sa.Text, sa.ForeignKey("case_run.case_run_id"), nullable=False),
    sa.Column("logical_task_id", sa.Text, nullable=False),
    sa.Column("plan_revision", sa.Integer, nullable=False),
    sa.Column("program_version", sa.Text, nullable=False),
    sa.Column("input_hash", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("output_hash", sa.Text, nullable=False),
    sa.Column("output_artifact_refs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("error_code", sa.Text, nullable=True),
    _created_at(),
    # First valid commit wins (ADR-12 clarification).
    sa.UniqueConstraint("case_run_id", "logical_task_id", "input_hash", "program_version", "plan_revision"),
    sa.CheckConstraint(
        "status IN ('SUCCEEDED', 'FAILED_RETRYABLE', 'FAILED_PERMANENT', 'CANCELLED')", name="status"
    ),
)

task_attempt = sa.Table(
    "task_attempt",
    metadata,
    sa.Column("attempt_id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("case_run_id", sa.Text, sa.ForeignKey("case_run.case_run_id"), nullable=False),
    sa.Column("logical_task_id", sa.Text, nullable=False),
    sa.Column("plan_revision", sa.Integer, nullable=False),
    sa.Column("program_version", sa.Text, nullable=False),
    sa.Column("input_hash", sa.Text, nullable=False),
    sa.Column("output_hash", sa.Text, nullable=False),
    sa.Column("output_artifact_refs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column(
        "task_execution_id", sa.Text, sa.ForeignKey("task_execution.task_execution_id"), nullable=False
    ),
    _created_at(),
    sa.CheckConstraint("outcome IN ('COMMITTED', 'DUPLICATE', 'CONFLICT')", name="outcome"),
)


release_manifest = sa.Table(
    "release_manifest",
    metadata,
    sa.Column("manifest_ref", sa.Text, primary_key=True),  # "<release_id>@<checksum[:12]>"
    sa.Column("release_id", sa.Text, nullable=False),
    sa.Column("checksum", sa.Text, nullable=False),
    sa.Column("artifact_ref", sa.Text, nullable=False),  # frozen manifest snapshot in P3
    _created_at(),
    sa.CheckConstraint("checksum ~ '^[0-9a-f]{64}$'", name="checksum_hex"),
)
