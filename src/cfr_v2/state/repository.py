"""P2 repository: every write is an insert, every insert is idempotent.

A repository wraps one SQLAlchemy ``Session``; the caller owns the transaction.
Inside workflows that session comes from a DBOS transaction step, so a write
commits together with its step checkpoint (LDR-0002).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cfr_v2.contracts import (
    AdmissionKey,
    AdmissionRecord,
    AdmissionStatus,
    ArtifactMetadata,
    Classification,
    IngestionRequest,
    TaskResult,
    canonical_hash,
    sha256_from_ref,
)

from . import schema as t

# Fixed namespace so case and run ids are reproducible from the admission key.
_ID_NAMESPACE = uuid.UUID("6f1c2a52-6a8e-4c5e-9d0e-2b7f3f6b9c11")


def case_id_for(source_system: str, source_case_id: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"case:{source_system}:{source_case_id}"))


def case_run_id_for(key: AdmissionKey) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"run:{key.source_system}:{key.source_case_id}:{key.ingestion_revision}"))


class CommitOutcome(StrEnum):
    COMMITTED = "COMMITTED"  # this attempt's result is the logical task result
    DUPLICATE = "DUPLICATE"  # identical result already committed
    CONFLICT = "CONFLICT"  # a different result was committed first; this one is audited only


@dataclass(frozen=True)
class TaskCommit:
    outcome: CommitOutcome
    task_execution_id: str
    committed_output_hash: str


class ArtifactConflictError(RuntimeError):
    """The same content was registered with different metadata."""


class CaseStateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- admission (C01) -------------------------------------------------------------

    def admit(
        self,
        request: IngestionRequest,
        *,
        request_ref: str,
        release_manifest_ref: str,
    ) -> AdmissionRecord:
        """Admit a request, or record it as a duplicate of the existing run."""
        key = request.key
        case_id = self._ensure_case(key)
        run_id = case_run_id_for(key)
        request_hash = canonical_hash(request)

        created = self.session.execute(
            insert(t.case_run)
            .values(
                case_run_id=run_id,
                case_id=case_id,
                ingestion_revision=key.ingestion_revision,
                release_manifest_ref=release_manifest_ref,
                lane=request.lane.value,
                request_ref=request_ref,
            )
            .on_conflict_do_nothing(index_elements=["case_id", "ingestion_revision"])
            .returning(t.case_run.c.case_run_id)
        ).scalar_one_or_none()

        if created is not None:
            status, reason = AdmissionStatus.ACCEPTED, None
            self.record_state(run_id, "ADMITTED")
        else:
            original_hash = self.session.execute(
                sa.select(t.admission.c.request_hash).where(
                    t.admission.c.case_run_id == run_id, t.admission.c.status == AdmissionStatus.ACCEPTED.value
                )
            ).scalar_one()
            status = AdmissionStatus.DUPLICATE
            reason = None if original_hash == request_hash else "content differs from accepted admission"

        return self._insert_admission(key, case_id, status, run_id, request_hash, reason)

    def reject(self, key: AdmissionKey, *, reason: str, request_hash: str) -> AdmissionRecord:
        case_id = self._ensure_case(key)
        return self._insert_admission(key, case_id, AdmissionStatus.REJECTED, None, request_hash, reason)

    def admissions_for(self, key: AdmissionKey) -> list[AdmissionRecord]:
        rows = self.session.execute(
            sa.select(t.admission)
            .where(
                t.admission.c.case_id == case_id_for(key.source_system, key.source_case_id),
                t.admission.c.ingestion_revision == key.ingestion_revision,
            )
            .order_by(t.admission.c.created_at, t.admission.c.admission_id)
        ).mappings()
        return [
            AdmissionRecord(
                admission_id=r["admission_id"],
                key=key,
                status=AdmissionStatus(r["status"]),
                case_id=r["case_id"],
                case_run_id=r["case_run_id"],
                reason=r["reason"],
            )
            for r in rows
        ]

    def _ensure_case(self, key: AdmissionKey) -> str:
        case_id = case_id_for(key.source_system, key.source_case_id)
        self.session.execute(
            insert(t.case)
            .values(case_id=case_id, source_system=key.source_system, source_case_id=key.source_case_id)
            .on_conflict_do_nothing(index_elements=["case_id"])
        )
        return case_id

    def _insert_admission(
        self,
        key: AdmissionKey,
        case_id: str,
        status: AdmissionStatus,
        run_id: str | None,
        request_hash: str,
        reason: str | None,
    ) -> AdmissionRecord:
        record = AdmissionRecord(
            admission_id=str(uuid.uuid4()),
            key=key,
            status=status,
            case_id=case_id,
            case_run_id=run_id,
            reason=reason,
        )
        self.session.execute(
            insert(t.admission).values(
                admission_id=record.admission_id,
                case_id=case_id,
                ingestion_revision=key.ingestion_revision,
                status=status.value,
                case_run_id=run_id,
                request_hash=request_hash,
                reason=reason,
            )
        )
        return record

    # -- case run state (HD-04) ------------------------------------------------------

    def record_state(self, case_run_id: str, state: str, detail: dict | None = None) -> bool:
        """Append a coarse state; returns False if the run already passed through it."""
        inserted = self.session.execute(
            insert(t.case_run_event)
            .values(case_run_id=case_run_id, state=state, detail=detail or {})
            .on_conflict_do_nothing(index_elements=["case_run_id", "state"])
            .returning(t.case_run_event.c.event_id)
        ).scalar_one_or_none()
        return inserted is not None

    def current_state(self, case_run_id: str) -> str | None:
        return self.session.execute(
            sa.select(t.case_run_event.c.state)
            .where(t.case_run_event.c.case_run_id == case_run_id)
            .order_by(t.case_run_event.c.event_id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def state_history(self, case_run_id: str) -> list[str]:
        return list(
            self.session.execute(
                sa.select(t.case_run_event.c.state)
                .where(t.case_run_event.c.case_run_id == case_run_id)
                .order_by(t.case_run_event.c.event_id)
            ).scalars()
        )

    # -- artifacts (P3 metadata) -----------------------------------------------------

    def register_artifact(self, meta: ArtifactMetadata, *, bucket: str, key: str) -> None:
        digest = sha256_from_ref(meta.ref)
        inserted = self.session.execute(
            insert(t.artifact)
            .values(
                sha256=digest,
                bucket=bucket,
                key=key,
                media_type=meta.media_type,
                classification=meta.classification.value,
                size_bytes=meta.size_bytes,
            )
            .on_conflict_do_nothing(index_elements=["sha256"])
            .returning(t.artifact.c.sha256)
        ).scalar_one_or_none()
        if inserted is not None:
            return
        existing = self.session.execute(sa.select(t.artifact).where(t.artifact.c.sha256 == digest)).mappings().one()
        if (existing["classification"], existing["bucket"], existing["size_bytes"]) != (
            meta.classification.value,
            bucket,
            meta.size_bytes,
        ):
            raise ArtifactConflictError(f"artifact {meta.ref} already registered with different metadata")

    def artifact_location(self, ref: str) -> tuple[str, str, Classification] | None:
        row = self.session.execute(
            sa.select(t.artifact.c.bucket, t.artifact.c.key, t.artifact.c.classification).where(
                t.artifact.c.sha256 == sha256_from_ref(ref)
            )
        ).one_or_none()
        return None if row is None else (row.bucket, row.key, Classification(row.classification))

    # -- logical task commits (ADR-12 first-valid-commit-wins) ---------------------

    def commit_task_result(
        self,
        *,
        case_run_id: str,
        logical_task_id: str,
        plan_revision: int,
        program_version: str,
        input_hash: str,
        result: TaskResult,
    ) -> TaskCommit:
        refs = list(result.output_artifact_refs)
        output_hash = canonical_hash(
            {"status": result.status.value, "output_artifact_refs": refs, "error_code": result.error_code}
        )
        identity = dict(
            case_run_id=case_run_id,
            logical_task_id=logical_task_id,
            plan_revision=plan_revision,
            program_version=program_version,
            input_hash=input_hash,
        )
        new_id = str(uuid.uuid4())
        inserted = self.session.execute(
            insert(t.task_execution)
            .values(
                task_execution_id=new_id,
                status=result.status.value,
                output_hash=output_hash,
                output_artifact_refs=refs,
                error_code=result.error_code,
                **identity,
            )
            .on_conflict_do_nothing(
                index_elements=["case_run_id", "logical_task_id", "input_hash", "program_version", "plan_revision"]
            )
            .returning(t.task_execution.c.task_execution_id)
        ).scalar_one_or_none()

        if inserted is not None:
            commit = TaskCommit(CommitOutcome.COMMITTED, inserted, output_hash)
        else:
            row = self.session.execute(
                sa.select(t.task_execution.c.task_execution_id, t.task_execution.c.output_hash).where(
                    *(t.task_execution.c[k] == v for k, v in identity.items())
                )
            ).one()
            outcome = CommitOutcome.DUPLICATE if row.output_hash == output_hash else CommitOutcome.CONFLICT
            commit = TaskCommit(outcome, row.task_execution_id, row.output_hash)

        self.session.execute(
            insert(t.task_attempt).values(
                output_hash=output_hash,
                output_artifact_refs=refs,
                outcome=commit.outcome.value,
                task_execution_id=commit.task_execution_id,
                **identity,
            )
        )
        return commit

    def task_attempts(self, case_run_id: str, logical_task_id: str) -> list[str]:
        return list(
            self.session.execute(
                sa.select(t.task_attempt.c.outcome)
                .where(t.task_attempt.c.case_run_id == case_run_id, t.task_attempt.c.logical_task_id == logical_task_id)
                .order_by(t.task_attempt.c.attempt_id)
            ).scalars()
        )

    def committed_tasks(self, case_run_id: str) -> list[str]:
        return list(
            self.session.execute(
                sa.select(t.task_execution.c.logical_task_id)
                .where(t.task_execution.c.case_run_id == case_run_id)
                .order_by(t.task_execution.c.created_at, t.task_execution.c.logical_task_id)
            ).scalars()
        )
