"""C02 outputs: Pass 1 allegations, baseline profile, Pass 2 grounded claims."""
from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from ._base import SHA256_PATTERN, Contract
from .artifacts import ArtifactRef
from .hashing import canonical_hash
from .intake import SpanRef


class ClaimScope(StrEnum):
    AGENT = "agent"
    CUSTOMER = "customer"
    SYSTEM = "system"
    MIXED = "mixed"


class StatementKind(StrEnum):
    CLAIM = "CLAIM"  # what allegedly went wrong
    IMPACT = "IMPACT"  # harm the customer reports


class Allegation(Contract):
    """Pass 1 output: what the customer alleged, from the transcript only (ADR-03)."""

    allegation_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: StatementKind
    scope: ClaimScope
    spans: tuple[SpanRef, ...] = Field(min_length=1)
    issue_indicators: tuple[str, ...] = ()
    ambiguous: bool = False
    ambiguity_note: str | None = None
    materiality_hint: str | None = None  # no business definition assumed (HD-13)

    @model_validator(mode="after")
    def _ambiguity_note(self) -> "Allegation":
        if self.ambiguity_note and not self.ambiguous:
            raise ValueError("ambiguity_note given for a non-ambiguous allegation")
        return self


class TemporalQuality(StrEnum):
    HISTORICAL_VERIFIED = "HISTORICAL_VERIFIED"
    CURRENT_ONLY = "CURRENT_ONLY"
    UNKNOWN = "UNKNOWN"


class ObservedFact(Contract):
    """One source fact with provenance (ADR-02 LUMI access contract)."""

    name: str = Field(min_length=1)
    value: JsonValue
    source: str = Field(min_length=1)  # e.g. axp-lumi.dw.cust360_customer_demographics
    query_version: str = Field(min_length=1)
    observed_at: AwareDatetime
    temporal_quality: TemporalQuality = TemporalQuality.CURRENT_ONLY


class BaselineProfile(Contract):
    case_id: str
    purpose: str = Field(min_length=1)
    allowlist_version: str = Field(min_length=1)
    facts: tuple[ObservedFact, ...] = ()


class EvidenceNeed(Contract):
    need_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    capability_hint: str | None = None
    proposition: str | None = None  # e.g. a CCP statement to verify against procedure


class GroundingStatus(StrEnum):
    GROUNDED = "GROUNDED"
    PARTIAL = "PARTIAL"
    UNGROUNDED = "UNGROUNDED"
    EVENT_TIME_APPROXIMATE = "EVENT_TIME_APPROXIMATE"


class EntityRef(Contract):
    entity_type: str = Field(min_length=1)  # account, product, transaction, ...
    reference: str = Field(min_length=1)


class GroundedClaim(Contract):
    claim_id: str = Field(min_length=1)
    allegation_ids: tuple[str, ...] = Field(min_length=1)
    statement: str = Field(min_length=1)
    entities: tuple[EntityRef, ...] = ()
    event_window_start: AwareDatetime | None = None
    event_window_end: AwareDatetime | None = None
    evidence_needs: tuple[EvidenceNeed, ...] = ()
    grounding_status: GroundingStatus

    @model_validator(mode="after")
    def _window_ordered(self) -> "GroundedClaim":
        s, e = self.event_window_start, self.event_window_end
        if s is not None and e is not None and e < s:
            raise ValueError("event window end precedes start")
        return self


class ClaimSetRevision(Contract):
    """Immutable claim set; ``claim_set_hash`` binds downstream selections and routes."""

    case_id: str
    run_id: str
    revision: int = Field(ge=0)
    previous_revision_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    allegations: tuple[Allegation, ...]
    claims: tuple[GroundedClaim, ...]
    baseline_profile_ref: ArtifactRef | None = None
    claim_set_hash: str = Field(pattern=SHA256_PATTERN)

    @staticmethod
    def compute_hash(allegations: tuple[Allegation, ...], claims: tuple[GroundedClaim, ...]) -> str:
        return canonical_hash(
            {
                "allegations": sorted(
                    (a.model_dump(mode="json") for a in allegations), key=lambda a: a["allegation_id"]
                ),
                "claims": sorted((c.model_dump(mode="json") for c in claims), key=lambda c: c["claim_id"]),
            }
        )

    @model_validator(mode="after")
    def _consistent(self) -> "ClaimSetRevision":
        allegation_ids = {a.allegation_id for a in self.allegations}
        if len(allegation_ids) != len(self.allegations):
            raise ValueError("allegation ids must be unique")
        if len({c.claim_id for c in self.claims}) != len(self.claims):
            raise ValueError("claim ids must be unique")
        for claim in self.claims:
            missing = set(claim.allegation_ids) - allegation_ids
            if missing:
                raise ValueError(f"claim {claim.claim_id} references unknown allegations {sorted(missing)}")
        if self.claim_set_hash != self.compute_hash(self.allegations, self.claims):
            raise ValueError("claim_set_hash does not match content")
        return self

    def claim(self, claim_id: str) -> GroundedClaim | None:
        return next((c for c in self.claims if c.claim_id == claim_id), None)
