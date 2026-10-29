"""C04 output: per-claim candidate verticals (ADR-07)."""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from ._base import SHA256_PATTERN, Contract
from .intake import SpanRef


class RouteSource(StrEnum):
    MODEL = "MODEL"
    KEYWORD_BASELINE = "KEYWORD_BASELINE"


class CandidateRoute(Contract):
    vertical_id: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    source: RouteSource
    cue_spans: tuple[SpanRef, ...] = ()
    rationale: str | None = None


class ClaimRoutingOutcome(StrEnum):
    ROUTED = "ROUTED"
    UNMAPPED = "UNMAPPED"  # concrete Amex failure outside all verticals (HD-18)
    NO_ELIGIBLE_VERTICAL = "NO_ELIGIBLE_VERTICAL"  # no Amex failure alleged


class ClaimRouting(Contract):
    claim_id: str = Field(min_length=1)
    outcome: ClaimRoutingOutcome
    candidates: tuple[CandidateRoute, ...] = ()

    @model_validator(mode="after")
    def _outcome_payload(self) -> "ClaimRouting":
        if self.outcome is ClaimRoutingOutcome.ROUTED and not self.candidates:
            raise ValueError("ROUTED requires at least one candidate")
        if self.outcome is not ClaimRoutingOutcome.ROUTED and self.candidates:
            raise ValueError(f"{self.outcome} must not list candidates")
        verticals = [c.vertical_id for c in self.candidates]
        if len(verticals) != len(set(verticals)):
            raise ValueError("duplicate vertical in candidates")
        return self


class RoutingResult(Contract):
    claim_set_hash: str = Field(pattern=SHA256_PATTERN)
    program_id: str = Field(min_length=1)
    program_version: str = Field(min_length=1)
    claims: tuple[ClaimRouting, ...]

    @model_validator(mode="after")
    def _unique_claims(self) -> "RoutingResult":
        ids = [c.claim_id for c in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("each claim is routed once")
        return self

    def is_stale(self, current_claim_set_hash: str) -> bool:
        return self.claim_set_hash != current_claim_set_hash
