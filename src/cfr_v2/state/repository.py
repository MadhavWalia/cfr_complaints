"""P2 repository: every write is an insert, every insert is idempotent.

A repository wraps one open transaction (``Tx``); the caller owns commit or
rollback. Inside the runner, a stage's result, its next state and (for the last
stage) the job's completion commit in one transaction (LDR-0011).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum

from cfr_v2.contracts import (
    AdmissionKey,
    AdmissionRecord,
    AdmissionStatus,
    ArtifactMetadata,
    Classification,
    IngestionRequest,
    ReleaseManifest,
    TaskResult,
    canonical_hash,
    sha256_from_ref,
)

from .db import Tx

# Fixed namespace so case and run ids are reproducible from the admission key.
_ID_NAMESPACE = uuid.UUID("6f1c2a52-6a8e-4c5e-9d0e-2b7f3f6b9c11")

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


@dataclass(frozen=True)
class CommittedTask:
    logical_task_id: str
    input_hash: str
    program_version: str
    plan_revision: int
    status: str
    output_artifact_refs: tuple[str, ...]


class ArtifactConflictError(RuntimeError):
    """The same content was registered with different metadata."""


class ManifestConflictError(RuntimeError):
    """Two manifests with different content share a ref (checksum-prefix collision)."""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class CaseStateRepository:
    def __init__(self, tx: Tx) -> None:
        self.tx = tx

    # -- admission (C01) -------------------------------------------------------------

    def admit(
        self,
        request: IngestionRequest,
        *,
        request_ref: str,
        release_manifest_ref: str,
        admitted_detail: dict | None = None,
    ) -> AdmissionRecord:
        """Admit a request, or record it as a duplicate of the existing run.

        Duplicates compare ``content_hash`` (arrival time and lane excluded), so a
        re-fetch of the same case is flagged only when its content changed.
        """
        key = request.key
        case_id = self._ensure_case(key)
        run_id = case_run_id_for(key)
        request_hash = request.content_hash()

        created = self.tx.scalar(
            "INSERT INTO case_run (case_run_id, case_id, ingestion_revision, release_manifest_ref, lane, request_ref)"
            " VALUES (%(run)s, %(case)s, %(rev)s, %(manifest)s, %(lane)s, %(request)s)"
            " ON CONFLICT (case_id, ingestion_revision) DO NOTHING RETURNING case_run_id",
            {
                "run": run_id,
                "case": case_id,
                "rev": key.ingestion_revision,
                "manifest": release_manifest_ref,
                "lane": request.lane.value,
                "request": request_ref,
            },
        )
        if created is not None:
            status, reason = AdmissionStatus.ACCEPTED, None
            self.record_state(run_id, "ADMITTED", admitted_detail)
        else:
            original_hash = self.tx.scalar(
                "SELECT request_hash FROM admission WHERE case_run_id = %(run)s AND status = 'ACCEPTED'",
                {"run": run_id},
            )
            status = AdmissionStatus.DUPLICATE
            reason = None if original_hash == request_hash else "content differs from accepted admission"

        return self._insert_admission(key, case_id, status, run_id, request_hash, reason)

    def reject(self, key: AdmissionKey, *, reason: str, request_hash: str) -> AdmissionRecord:
        case_id = self._ensure_case(key)
        return self._insert_admission(key, case_id, AdmissionStatus.REJECTED, None, request_hash, reason)

    def admissions_for(self, key: AdmissionKey) -> list[AdmissionRecord]:
        rows = self.tx.rows(
            "SELECT admission_id, status, case_id, case_run_id, reason FROM admission"
            " WHERE case_id = %(case)s AND ingestion_revision = %(rev)s ORDER BY created_at, admission_id",
            {"case": case_id_for(key.source_system, key.source_case_id), "rev": key.ingestion_revision},
        )
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
        # No conflict target: case_id derives from (source_system, source_case_id), so a
        # concurrent insert may surface on either unique index; both mean "already exists".
        self.tx.execute(
            'INSERT INTO "case" (case_id, source_system, source_case_id) VALUES (%(case)s, %(sys)s, %(src)s)'
            " ON CONFLICT DO NOTHING",
            {"case": case_id, "sys": key.source_system, "src": key.source_case_id},
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
        self.tx.execute(
            "INSERT INTO admission (admission_id, case_id, ingestion_revision, status, case_run_id, request_hash, reason)"
            " VALUES (%(id)s, %(case)s, %(rev)s, %(status)s, %(run)s, %(hash)s, %(reason)s)",
            {
                "id": record.admission_id,
                "case": case_id,
                "rev": key.ingestion_revision,
                "status": status.value,
                "run": run_id,
                "hash": request_hash,
                "reason": reason,
            },
        )
        return record

    def case_run(self, case_run_id: str) -> dict | None:
        rows = self.tx.rows(
            "SELECT case_run_id, case_id, ingestion_revision, release_manifest_ref, lane, request_ref"
            " FROM case_run WHERE case_run_id = %(run)s",
            {"run": case_run_id},
        )
        return rows[0] if rows else None

    # -- case run state (HD-04) ------------------------------------------------------

    def record_state(self, case_run_id: str, state: str, detail: dict | None = None) -> bool:
        """Append a coarse state; returns False if the run already passed through it."""
        inserted = self.tx.scalar(
            "INSERT INTO case_run_event (case_run_id, state, detail) VALUES (%(run)s, %(state)s, %(detail)s::jsonb)"
            " ON CONFLICT (case_run_id, state) DO NOTHING RETURNING event_id",
            {"run": case_run_id, "state": state, "detail": _json(detail or {})},
        )
        return inserted is not None

    def current_state(self, case_run_id: str) -> str | None:
        return self.tx.scalar(
            "SELECT state FROM case_run_event WHERE case_run_id = %(run)s ORDER BY event_id DESC LIMIT 1",
            {"run": case_run_id},
        )

    def state_history(self, case_run_id: str) -> list[str]:
        return self.tx.column(
            "SELECT state FROM case_run_event WHERE case_run_id = %(run)s ORDER BY event_id", {"run": case_run_id}
        )

    # -- artifacts (P3 metadata) -----------------------------------------------------

    def register_artifact(self, meta: ArtifactMetadata, *, bucket: str, key: str) -> None:
        digest = sha256_from_ref(meta.ref)
        inserted = self.tx.scalar(
            "INSERT INTO artifact (sha256, bucket, key, media_type, classification, size_bytes)"
            " VALUES (%(sha)s, %(bucket)s, %(key)s, %(media)s, %(cls)s, %(size)s)"
            " ON CONFLICT (sha256, classification) DO NOTHING RETURNING sha256",
            {
                "sha": digest,
                "bucket": bucket,
                "key": key,
                "media": meta.media_type,
                "cls": meta.classification.value,
                "size": meta.size_bytes,
            },
        )
        if inserted is not None:
            return
        existing = self.tx.one(
            "SELECT bucket, size_bytes FROM artifact WHERE sha256 = %(sha)s AND classification = %(cls)s",
            {"sha": digest, "cls": meta.classification.value},
        )
        if existing != (bucket, meta.size_bytes):
            raise ArtifactConflictError(
                f"artifact {meta.ref} ({meta.classification.value}) already registered in another bucket"
            )

    def artifact_location(self, ref: str, classification: Classification) -> tuple[str, str] | None:
        row = self.tx.one(
            "SELECT bucket, key FROM artifact WHERE sha256 = %(sha)s AND classification = %(cls)s",
            {"sha": sha256_from_ref(ref), "cls": classification.value},
        )
        return None if row is None else (row[0], row[1])

    # -- release manifests (P4 pinning) ---------------------------------------------

    def register_manifest(self, manifest: ReleaseManifest, *, artifact_ref: str) -> None:
        """Record a manifest snapshot; idempotent for identical content."""
        inserted = self.tx.scalar(
            "INSERT INTO release_manifest (manifest_ref, release_id, checksum, artifact_ref)"
            " VALUES (%(ref)s, %(release)s, %(checksum)s, %(artifact)s)"
            " ON CONFLICT (manifest_ref) DO NOTHING RETURNING manifest_ref",
            {
                "ref": manifest.ref,
                "release": manifest.release_id,
                "checksum": manifest.checksum,
                "artifact": artifact_ref,
            },
        )
        if inserted is None:
            existing = self.tx.scalar(
                "SELECT checksum FROM release_manifest WHERE manifest_ref = %(ref)s", {"ref": manifest.ref}
            )
            if existing != manifest.checksum:
                raise ManifestConflictError(f"manifest ref {manifest.ref} already registered with other content")

    def manifest_artifact_ref(self, manifest_ref: str) -> str | None:
        return self.tx.scalar(
            "SELECT artifact_ref FROM release_manifest WHERE manifest_ref = %(ref)s", {"ref": manifest_ref}
        )

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
        identity = {
            "run": case_run_id,
            "task": logical_task_id,
            "rev": plan_revision,
            "version": program_version,
            "input": input_hash,
        }
        inserted = self.tx.scalar(
            "INSERT INTO task_execution (task_execution_id, case_run_id, logical_task_id, plan_revision,"
            " program_version, input_hash, status, output_hash, output_artifact_refs, error_code)"
            " VALUES (%(id)s, %(run)s, %(task)s, %(rev)s, %(version)s, %(input)s, %(status)s, %(out)s,"
            " %(refs)s::jsonb, %(error)s)"
            " ON CONFLICT (case_run_id, logical_task_id, input_hash, program_version, plan_revision) DO NOTHING"
            " RETURNING task_execution_id",
            {
                **identity,
                "id": str(uuid.uuid4()),
                "status": result.status.value,
                "out": output_hash,
                "refs": _json(refs),
                "error": result.error_code,
            },
        )
        if inserted is not None:
            commit = TaskCommit(CommitOutcome.COMMITTED, inserted, output_hash)
        else:
            row = self.tx.one(
                "SELECT task_execution_id, output_hash FROM task_execution"
                " WHERE case_run_id = %(run)s AND logical_task_id = %(task)s AND plan_revision = %(rev)s"
                " AND program_version = %(version)s AND input_hash = %(input)s",
                identity,
            )
            assert row is not None
            outcome = CommitOutcome.DUPLICATE if row[1] == output_hash else CommitOutcome.CONFLICT
            commit = TaskCommit(outcome, row[0], row[1])

        self.tx.execute(
            "INSERT INTO task_attempt (case_run_id, logical_task_id, plan_revision, program_version, input_hash,"
            " output_hash, output_artifact_refs, outcome, task_execution_id)"
            " VALUES (%(run)s, %(task)s, %(rev)s, %(version)s, %(input)s, %(out)s, %(refs)s::jsonb,"
            " %(outcome)s, %(exec)s)",
            {
                **identity,
                "out": output_hash,
                "refs": _json(refs),
                "outcome": commit.outcome.value,
                "exec": commit.task_execution_id,
            },
        )
        return commit

    def committed_results(self, case_run_id: str) -> list[CommittedTask]:
        rows = self.tx.all(
            "SELECT logical_task_id, input_hash, program_version, plan_revision, status, output_artifact_refs"
            " FROM task_execution WHERE case_run_id = %(run)s ORDER BY created_at, logical_task_id",
            {"run": case_run_id},
        )
        return [CommittedTask(r[0], r[1], r[2], r[3], r[4], tuple(r[5])) for r in rows]

    def task_attempts(self, case_run_id: str, logical_task_id: str) -> list[str]:
        return self.tx.column(
            "SELECT outcome FROM task_attempt WHERE case_run_id = %(run)s AND logical_task_id = %(task)s"
            " ORDER BY attempt_id",
            {"run": case_run_id, "task": logical_task_id},
        )

    def committed_tasks(self, case_run_id: str) -> list[str]:
        return [c.logical_task_id for c in self.committed_results(case_run_id)]
