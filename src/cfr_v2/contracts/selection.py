"""C03 output: primary claim selection bound to a claim-set hash (ADR-16)."""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from ._base import SHA256_PATTERN, Contract
from .intake import SpanRef


class SelectionStatus(StrEnum):
    SELECTED = "SELECTED"
    AMBIGUOUS = "AMBIGUOUS"
    NO_ELIGIBLE_CLAIM = "NO_ELIGIBLE_CLAIM"


class PrimarySelection(Contract):
    status: SelectionStatus
    claim_id: str | None = None
    candidate_claim_ids: tuple[str, ...] = ()  # set when AMBIGUOUS
    supporting_spans: tuple[SpanRef, ...] = ()
    rationale: str = Field(min_length=1)
    program_id: str = Field(min_length=1)
    program_version: str = Field(min_length=1)
    claim_set_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _status_payload(self) -> "PrimarySelection":
        if self.status is SelectionStatus.SELECTED:
            if not self.claim_id:
                raise ValueError("SELECTED requires claim_id")
            if self.candidate_claim_ids:
                raise ValueError("SELECTED must not list candidates")
        elif self.claim_id is not None:
            raise ValueError(f"{self.status} must not set claim_id")
        if self.status is SelectionStatus.AMBIGUOUS and len(self.candidate_claim_ids) < 2:
            raise ValueError("AMBIGUOUS requires at least two candidate claims")
        if self.status is SelectionStatus.NO_ELIGIBLE_CLAIM and self.candidate_claim_ids:
            raise ValueError("NO_ELIGIBLE_CLAIM must not list candidates")
        return self

    def is_stale(self, current_claim_set_hash: str) -> bool:
        return self.claim_set_hash != current_claim_set_hash
