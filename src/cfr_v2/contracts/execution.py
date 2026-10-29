from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ._base import SHA256_PATTERN, Contract
from .artifacts import ArtifactRef


class Lane(StrEnum):
    BATCH = "batch"
    REALTIME = "realtime"


class AuthorizationContext(Contract):
    principal: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    allowed_classifications: frozenset[Literal["RESTRICTED", "INTERNAL"]] = frozenset({"INTERNAL"})


class ExecutionBudget(Contract):
    """Per-case-run limits (HD-09). ``None`` means unlimited for that dimension."""

    max_model_calls: int | None = Field(default=None, ge=0)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_plan_revisions: int = Field(default=2, ge=0)
    max_evidence_expansions: int = Field(default=2, ge=0)


class TaskEnvelope(Contract):
    """Input to every component function; workflow steps pass only this."""

    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    input_artifact_refs: tuple[ArtifactRef, ...] = ()
    input_snapshot_hash: str = Field(pattern=SHA256_PATTERN)
    release_manifest_ref: str = Field(min_length=1)
    authorization: AuthorizationContext
    idempotency_key: str = Field(min_length=1)
    lane: Lane = Lane.BATCH
    plan_revision: int = Field(default=0, ge=0)
    deadline_at: AwareDatetime
    trace_id: str | None = None


class TaskStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    CANCELLED = "CANCELLED"


class TaskResult(Contract):
    task_id: str = Field(min_length=1)
    status: TaskStatus
    output_artifact_refs: tuple[ArtifactRef, ...] = ()
    error_code: str | None = None
    execution_metadata_ref: ArtifactRef | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "TaskResult":
        failed = self.status in (TaskStatus.FAILED_RETRYABLE, TaskStatus.FAILED_PERMANENT)
        if failed and not self.error_code:
            raise ValueError("failed results require error_code")
        if self.status is TaskStatus.SUCCEEDED and self.error_code:
            raise ValueError("succeeded results must not carry error_code")
        return self
