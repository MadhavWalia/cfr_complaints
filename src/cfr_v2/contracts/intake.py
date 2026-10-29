from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from ._base import SHA256_PATTERN, Contract
from .artifacts import ArtifactRef
from .execution import Lane


class AdmissionKey(Contract):
    source_system: str = Field(min_length=1)
    source_case_id: str = Field(min_length=1)
    ingestion_revision: int = Field(ge=0)


class IngestionRequest(Contract):
    """What a batch coordinator or API hands to C01.

    ``case_metadata`` may be shown to models after redaction; ``evaluation_labels``
    (e.g. ``potl_defect_flag``, ``err_type``) are stored for evaluation only and must
    never reach model input (C01 design, OQ-13).
    """

    key: AdmissionKey
    raw_transcript_ref: ArtifactRef | None
    case_note_refs: dict[str, ArtifactRef] = Field(default_factory=dict)
    case_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    evaluation_labels: dict[str, JsonValue] = Field(default_factory=dict)
    source_interaction_ids: tuple[str, ...] = ()
    linkage_quality: str | None = None
    lane: Lane = Lane.BATCH
    received_at: AwareDatetime

    @model_validator(mode="after")
    def _labels_disjoint_from_metadata(self) -> "IngestionRequest":
        overlap = set(self.case_metadata) & set(self.evaluation_labels)
        if overlap:
            raise ValueError(f"fields cannot be both metadata and labels: {sorted(overlap)}")
        return self


class AdmissionStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


class AdmissionRecord(Contract):
    admission_id: str
    key: AdmissionKey
    status: AdmissionStatus
    case_id: str
    case_run_id: str | None
    reason: str | None = None

    @model_validator(mode="after")
    def _run_matches_status(self) -> "AdmissionRecord":
        if self.status is AdmissionStatus.REJECTED and self.case_run_id is not None:
            raise ValueError("rejected admissions have no case run")
        if self.status is not AdmissionStatus.REJECTED and self.case_run_id is None:
            raise ValueError("accepted/duplicate admissions reference a case run")
        if self.status is AdmissionStatus.REJECTED and not self.reason:
            raise ValueError("rejected admissions require a reason")
        return self


class InteractionKind(StrEnum):
    CONCATENATED = "CONCATENATED"  # V1 msg_tx2 (LDR-0005)
    CALL = "CALL"
    CHAT = "CHAT"


class TranscriptInteraction(Contract):
    interaction_id: str = Field(min_length=1)
    kind: InteractionKind
    occurred_at: AwareDatetime | None = None
    linkage_quality: str | None = None
    source_interaction_ids: tuple[str, ...] = ()
    text_sha256: str = Field(pattern=SHA256_PATTERN)
    length: int = Field(ge=0)


class CanonicalTranscript(Contract):
    """Ordered interactions of one case; text bodies live in artifacts."""

    case_id: str
    interactions: tuple[TranscriptInteraction, ...] = Field(min_length=1)
    normalization_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_interaction_ids(self) -> "CanonicalTranscript":
        ids = [i.interaction_id for i in self.interactions]
        if len(ids) != len(set(ids)):
            raise ValueError("interaction ids must be unique")
        return self


class SpanRef(Contract):
    """Half-open character range ``[start, end)`` in an interaction's redacted text."""

    interaction_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> "SpanRef":
        if self.end <= self.start:
            raise ValueError("span end must be greater than start")
        return self


class RedactionSpan(Contract):
    original_start: int = Field(ge=0)
    original_end: int = Field(ge=0)
    redacted_start: int = Field(ge=0)
    redacted_end: int = Field(ge=0)
    entity_type: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "RedactionSpan":
        if self.original_end <= self.original_start or self.redacted_end <= self.redacted_start:
            raise ValueError("redaction spans must be non-empty")
        return self


class RedactionMap(Contract):
    """Offset translation between original and redacted text (LDR-0004). RESTRICTED."""

    interaction_id: str
    original_sha256: str = Field(pattern=SHA256_PATTERN)
    redacted_sha256: str = Field(pattern=SHA256_PATTERN)
    spans: tuple[RedactionSpan, ...] = ()
    redaction_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _sorted_non_overlapping(self) -> "RedactionMap":
        prev_o = prev_r = -1
        for s in self.spans:
            if s.original_start < prev_o or s.redacted_start < prev_r:
                raise ValueError("redaction spans must be sorted and non-overlapping")
            prev_o, prev_r = s.original_end, s.redacted_end
        return self

    def to_original(self, start: int, end: int) -> tuple[int, int]:
        """Translate a half-open redacted-text range to the original text.

        A boundary falling inside a redaction token expands to cover the whole
        original redacted range, so a span never cuts a sensitive value in half.
        """
        if end <= start:
            raise ValueError("range end must be greater than start")
        return self._start_to_original(start), self._end_to_original(end)

    def _start_to_original(self, p: int) -> int:
        shift = 0
        for s in self.spans:
            if p < s.redacted_start:
                break
            if p < s.redacted_end:
                return s.original_start
            shift = s.original_end - s.redacted_end
        return p + shift

    def _end_to_original(self, p: int) -> int:
        shift = 0
        for s in self.spans:
            if p <= s.redacted_start:
                break
            if p <= s.redacted_end:
                return s.original_end
            shift = s.original_end - s.redacted_end
        return p + shift


class CaseNote(Contract):
    case_id: str
    note_type: str = Field(min_length=1)  # e.g. case_note, ccp_note, latest_rslv_note
    text_ref: ArtifactRef
