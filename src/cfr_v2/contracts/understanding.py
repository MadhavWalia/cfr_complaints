"""C02 outputs: Pass 1 allegations, requests and agent statements, and Pass 2 grounded claims. The baseline C02 reads is in ``baseline.py``."""

from __future__ import annotations

from datetime import date
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
    THIRD_PARTY = "third_party"  # a merchant, partner or another company
    MIXED = "mixed"


class StatementKind(StrEnum):
    CLAIM = "CLAIM"  # what allegedly went wrong
    IMPACT = "IMPACT"  # harm the customer reports


class _Ambiguity(Contract):
    ambiguous: bool = False
    ambiguity_note: str | None = None

    @model_validator(mode="after")
    def _ambiguity_note(self) -> _Ambiguity:
        if self.ambiguity_note and not self.ambiguous:
            raise ValueError("ambiguity_note given for a non-ambiguous item")
        return self


class DetailType(StrEnum):
    WHEN = "when"
    AMOUNT = "amount"
    MERCHANT_OR_PRODUCT = "merchant_or_product"
    EARLIER_CONTACT = "earlier_contact"
    OTHER = "other"


class AllegationDetail(Contract):
    """A specific fact the customer gives about one allegation, as stated (Pass 2 grounds it)."""

    detail_type: DetailType
    text: str = Field(min_length=1)
    span: SpanRef


class Allegation(_Ambiguity):
    """Pass 1 output: what the customer alleged, from the transcript only (ADR-03)."""

    allegation_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: StatementKind
    scope: ClaimScope
    spans: tuple[SpanRef, ...] = Field(min_length=1)
    details: tuple[AllegationDetail, ...] = ()
    issue_indicators: tuple[str, ...] = ()
    materiality_hint: str | None = None  # no business definition assumed (HD-13)


class RequestType(StrEnum):
    REFUND_OR_CREDIT = "refund_or_credit"
    FEE_REVERSAL = "fee_reversal"
    CANCEL_OR_CLOSE = "cancel_or_close"
    REPLACE_CARD = "replace_card"
    EXPLANATION = "explanation"
    ESCALATION = "escalation"
    CALLBACK = "callback"
    OTHER = "other"


class CustomerRequest(_Ambiguity):
    """Pass 1 output: one thing the customer asked American Express to do."""

    request_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    request_type: RequestType
    spans: tuple[SpanRef, ...] = Field(min_length=1)
    allegation_ids: tuple[str, ...] = ()


class AgentStatementKind(StrEnum):
    FACT = "FACT"  # information stated as true
    ACTION = "ACTION"  # something the agent says they did or are doing
    COMMITMENT = "COMMITMENT"  # something promised for later
    ADVICE = "ADVICE"  # something the agent told the customer to do


class AgentStatement(_Ambiguity):
    """Pass 1 output: something an agent said or did that can be checked against procedure or records.

    Pass 2 turns these into evidence needs (C02 LLD); the V1 rule was "every CCP statement is checked".
    """

    statement_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: AgentStatementKind
    spans: tuple[SpanRef, ...] = Field(min_length=1)
    allegation_ids: tuple[str, ...] = ()
    request_ids: tuple[str, ...] = ()


class DroppedReason(StrEnum):
    NO_QUOTE = "NO_QUOTE"  # the model gave no quote
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"  # no quote occurs in the redacted transcript
    INVALID = "INVALID"  # fails its contract (e.g. empty text)


class Pass1List(StrEnum):
    ALLEGATIONS = "allegations"
    DETAILS = "details"
    REQUESTS = "requests"
    AGENT_STATEMENTS = "agent_statements"


class DroppedItem(Contract):
    """An extracted item that failed deterministic validation. Carries no text (it may quote the call)."""

    item_list: Pass1List
    index: int = Field(ge=0)  # position in the model's list (for details: the allegation's)
    detail_index: int | None = Field(default=None, ge=0)  # position within that allegation's details
    reason: DroppedReason


class Pass1Result(Contract):
    """Pass 1 result for one case run: the C02 stage artifact until Pass 2 and claim sets land.

    Invalid items are dropped, never the whole result (C02 LLD); ``dropped`` records them.
    Links point only at kept items.
    """

    case_id: str = Field(min_length=1)
    program_id: str = Field(min_length=1)
    program_version: str = Field(min_length=1)
    transcript_ref: ArtifactRef | None  # None: the case has no transcript, and no model was called
    allegations: tuple[Allegation, ...] = ()
    requests: tuple[CustomerRequest, ...] = ()
    agent_statements: tuple[AgentStatement, ...] = ()
    dropped: tuple[DroppedItem, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Pass1Result:
        allegation_ids = [a.allegation_id for a in self.allegations]
        request_ids = [r.request_id for r in self.requests]
        statement_ids = [s.statement_id for s in self.agent_statements]
        ids = allegation_ids + request_ids + statement_ids
        if len(set(ids)) != len(ids):
            raise ValueError("item ids must be unique")
        linked_allegations = {i for r in self.requests for i in r.allegation_ids} | {
            i for s in self.agent_statements for i in s.allegation_ids
        }
        linked_requests = {i for s in self.agent_statements for i in s.request_ids}
        if not linked_allegations <= set(allegation_ids) or not linked_requests <= set(request_ids):
            raise ValueError("links must point at kept items")
        if self.transcript_ref is None and (ids or self.dropped):
            raise ValueError("a case without a transcript has nothing extracted")
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


SOURCE_ORIGIN_PATTERN = r"^[sr][1-9][0-9]*$"  # an agent statement (s1) or a customer request (r1)


class EvidenceNeed(Contract):
    """Something an investigator would check for one claim (Pass 2)."""

    need_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_type: str = Field(min_length=1)  # an active id of the pinned evidence source taxonomy
    proposition: str | None = None  # e.g. what an agent said, to verify against records or procedure
    origin: str | None = Field(default=None, pattern=SOURCE_ORIGIN_PATTERN)


class OtherCheck(Contract):
    """An agent statement or request linked to no claim: still checked, outside primary selection (C03)."""

    check_id: str = Field(min_length=1)
    origin: str = Field(pattern=SOURCE_ORIGIN_PATTERN)
    description: str = Field(min_length=1)
    proposition: str | None = None
    source_types: tuple[str, ...] = Field(min_length=1)


class GroundingStatus(StrEnum):
    GROUNDED = "GROUNDED"
    PARTIAL = "PARTIAL"
    UNGROUNDED = "UNGROUNDED"
    EVENT_TIME_APPROXIMATE = "EVENT_TIME_APPROXIMATE"


class AccountResolution(StrEnum):
    RESOLVED = "RESOLVED"  # one account
    CANDIDATES = "CANDIDATES"  # several could fit; listed, case account first
    NONE = "NONE"  # about an account, but none can be named (e.g. no baseline)
    NOT_APPLICABLE = "NOT_APPLICABLE"  # not about an account (e.g. agent conduct)


class TimeQuality(StrEnum):
    EXACT = "EXACT"  # stated dates
    APPROXIMATE = "APPROXIMATE"  # relative or vague ("last week")
    UNKNOWN = "UNKNOWN"  # no time given


def grounding_status_for(account: AccountResolution, time: TimeQuality, *, has_entities: bool) -> GroundingStatus:
    """The HLD summary status, derived so it can't disagree with the two fields it summarises."""
    account_known = account in (AccountResolution.RESOLVED, AccountResolution.NOT_APPLICABLE)
    if account_known and time is TimeQuality.EXACT:
        return GroundingStatus.GROUNDED
    if account_known and time is TimeQuality.APPROXIMATE:
        return GroundingStatus.EVENT_TIME_APPROXIMATE
    if account is AccountResolution.NONE and time is TimeQuality.UNKNOWN and not has_entities:
        return GroundingStatus.UNGROUNDED
    return GroundingStatus.PARTIAL


def account_resolution_for(refs: tuple[str, ...], *, about_an_account: bool) -> AccountResolution:
    if not about_an_account:
        return AccountResolution.NOT_APPLICABLE
    return {0: AccountResolution.NONE, 1: AccountResolution.RESOLVED}.get(len(refs), AccountResolution.CANDIDATES)


class EntityRef(Contract):
    entity_type: str = Field(min_length=1)  # product, merchant, transaction, fee, other
    reference: str = Field(min_length=1)  # as the customer stated it; never an identifier


class GroundedClaim(Contract):
    claim_id: str = Field(min_length=1)
    allegation_ids: tuple[str, ...] = Field(min_length=1)
    statement: str = Field(min_length=1)
    entities: tuple[EntityRef, ...] = ()
    account_refs: tuple[str, ...] = ()  # BaselineProfile refs (A1, ...); candidates in order, case account first
    account_resolution: AccountResolution = AccountResolution.NONE
    event_window_start: date | None = None
    event_window_end: date | None = None
    time_quality: TimeQuality = TimeQuality.UNKNOWN
    grounding_note: str | None = None
    evidence_needs: tuple[EvidenceNeed, ...] = ()
    grounding_status: GroundingStatus

    @model_validator(mode="after")
    def _consistent(self) -> GroundedClaim:
        s, e = self.event_window_start, self.event_window_end
        if s is not None and e is not None and e < s:
            raise ValueError("event window end precedes start")
        expected = {
            AccountResolution.RESOLVED: len(self.account_refs) == 1,
            AccountResolution.CANDIDATES: len(self.account_refs) > 1,
        }.get(self.account_resolution, not self.account_refs)
        if not expected:
            raise ValueError(f"{self.account_resolution.value} does not fit {len(self.account_refs)} account refs")
        derived = grounding_status_for(self.account_resolution, self.time_quality, has_entities=bool(self.entities))
        if self.grounding_status is not derived:
            raise ValueError(f"grounding_status must be {derived.value} for these fields")
        if len({n.need_id for n in self.evidence_needs}) != len(self.evidence_needs):
            raise ValueError("evidence need ids must be unique")
        return self


class Pass2Issue(StrEnum):
    UNCOVERED_ALLEGATION = "UNCOVERED_ALLEGATION"  # not in any claim: given a claim of its own by code
    REPEATED_ALLEGATION = "REPEATED_ALLEGATION"  # in two claims: kept in the first only
    EMPTY_CLAIM = "EMPTY_CLAIM"  # no known allegations left
    UNKNOWN_ACCOUNT_REF = "UNKNOWN_ACCOUNT_REF"  # not in the baseline: removed
    INVALID_WINDOW = "INVALID_WINDOW"  # unparseable or end before start: cleared
    WINDOW_AFTER_RECEIVED = "WINDOW_AFTER_RECEIVED"  # ends after the received date: cut back
    UNKNOWN_SOURCE_TYPE = "UNKNOWN_SOURCE_TYPE"  # not active in the taxonomy: need or check dropped
    UNKNOWN_ORIGIN = "UNKNOWN_ORIGIN"  # not a Pass 1 statement or request: origin removed
    NOT_COVERED = "NOT_COVERED"  # a statement or request with no need or check


class Pass2Finding(Contract):
    """A deterministic correction to Pass 2's output. Carries ids, not text."""

    issue: Pass2Issue
    ref: str | None = None  # claim position (c3), allegation, statement or request id, or source type


class ClaimSetRevision(Contract):
    """Immutable claim set; ``claim_set_hash`` binds downstream selections and routes.

    Holds Pass 1's lists unchanged (ADR-03) and Pass 2's claims and other checks.
    """

    case_id: str
    run_id: str
    revision: int = Field(ge=0)
    previous_revision_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    allegations: tuple[Allegation, ...]
    requests: tuple[CustomerRequest, ...] = ()
    agent_statements: tuple[AgentStatement, ...] = ()
    claims: tuple[GroundedClaim, ...]
    other_checks: tuple[OtherCheck, ...] = ()
    baseline_profile_ref: ArtifactRef | None = None  # None: no baseline; recorded as baseline absent
    case_received_date: date | None = None
    evidence_sources_version: str | None = None
    evidence_sources_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    findings: tuple[Pass2Finding, ...] = ()
    claim_set_hash: str = Field(pattern=SHA256_PATTERN)

    @staticmethod
    def compute_hash(
        allegations: tuple[Allegation, ...],
        claims: tuple[GroundedClaim, ...],
        requests: tuple[CustomerRequest, ...] = (),
        agent_statements: tuple[AgentStatement, ...] = (),
        other_checks: tuple[OtherCheck, ...] = (),
    ) -> str:
        def ordered(items, key):
            return sorted((i.model_dump(mode="json") for i in items), key=lambda d: d[key])

        body = {
            "allegations": ordered(allegations, "allegation_id"),
            "claims": ordered(claims, "claim_id"),
        }
        # Absent when empty, so claim sets without them keep their hash.
        for name, items, key in (
            ("requests", requests, "request_id"),
            ("agent_statements", agent_statements, "statement_id"),
            ("other_checks", other_checks, "check_id"),
        ):
            if items:
                body[name] = ordered(items, key)
        return canonical_hash(body)

    @model_validator(mode="after")
    def _consistent(self) -> ClaimSetRevision:
        allegation_ids = {a.allegation_id for a in self.allegations}
        if len(allegation_ids) != len(self.allegations):
            raise ValueError("allegation ids must be unique")
        if len({c.claim_id for c in self.claims}) != len(self.claims):
            raise ValueError("claim ids must be unique")
        for claim in self.claims:
            missing = set(claim.allegation_ids) - allegation_ids
            if missing:
                raise ValueError(f"claim {claim.claim_id} references unknown allegations {sorted(missing)}")
        origins = {r.request_id for r in self.requests} | {s.statement_id for s in self.agent_statements}
        used = {n.origin for c in self.claims for n in c.evidence_needs if n.origin} | {
            o.origin for o in self.other_checks
        }
        if not used <= origins:
            raise ValueError(f"evidence needs reference unknown origins {sorted(used - origins)}")
        if self.claim_set_hash != self.compute_hash(
            self.allegations, self.claims, self.requests, self.agent_statements, self.other_checks
        ):
            raise ValueError("claim_set_hash does not match content")
        return self

    def claim(self, claim_id: str) -> GroundedClaim | None:
        return next((c for c in self.claims if c.claim_id == claim_id), None)
