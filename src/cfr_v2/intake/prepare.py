"""Turn one source row into stored artifacts and an ``IngestionRequest`` (C01 steps 1, 3-5).

Every artifact is content-addressed, so preparing the same row twice yields the
same refs; nothing here touches the database.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cfr_v2.artifacts import ArtifactStore, StoredArtifact
from cfr_v2.contracts import (
    AdmissionKey,
    CanonicalTranscript,
    Classification,
    IngestionRequest,
    InteractionKind,
    Lane,
    TranscriptInteraction,
    canonical_json,
)
from cfr_v2.state import case_id_for

from .normalize import NORMALIZATION_VERSION, normalize_transcript
from .redaction import Masker, ProfanityFilter, redact
from .source import (
    LINKAGE_QUALITY_COLUMN,
    SOURCE_SYSTEM,
    TRANSCRIPT_COLUMN,
    case_metadata,
    evaluation_labels,
    source_case_id,
    source_interaction_ids,
    source_record_bytes,
)

# A re-run is always the same case (OQ-23).
INGESTION_REVISION = 0
INTERACTION_ID = "msg_tx2"
JSON = "application/json"
TEXT = "text/plain; charset=utf-8"


@dataclass(frozen=True)
class PreparedCase:
    request: IngestionRequest
    artifacts: tuple[StoredArtifact, ...]  # registered in the admission transaction

    @property
    def has_transcript(self) -> bool:
        return self.request.transcript_ref is not None


def admission_key(row: Mapping[str, Any]) -> AdmissionKey:
    return AdmissionKey(source_system=SOURCE_SYSTEM, source_case_id=source_case_id(row), ingestion_revision=INGESTION_REVISION)


def prepare_case(
    row: Mapping[str, Any],
    *,
    store: ArtifactStore,
    masker: Masker,
    profanity: ProfanityFilter,
    lane: Lane,
    received_at: datetime,
) -> PreparedCase:
    key = admission_key(row)
    original = normalize_transcript(row.get(TRANSCRIPT_COLUMN))
    redaction = redact(original, interaction_id=INTERACTION_ID, masker=masker, profanity=profanity) if original else None

    source = store.put(source_record_bytes(row), media_type=JSON, classification=Classification.RESTRICTED)
    artifacts = [source]
    transcript_ref = None
    if redaction is not None:
        original_a = store.put(original.encode("utf-8"), media_type=TEXT, classification=Classification.RESTRICTED)
        redacted_a = store.put(redaction.text.encode("utf-8"), media_type=TEXT, classification=Classification.INTERNAL)
        map_a = store.put(canonical_json(redaction.map), media_type=JSON, classification=Classification.RESTRICTED)
        transcript = CanonicalTranscript(
            case_id=case_id_for(key.source_system, key.source_case_id),
            interactions=(
                TranscriptInteraction(
                    interaction_id=INTERACTION_ID,
                    kind=InteractionKind.CONCATENATED,
                    linkage_quality=row.get(LINKAGE_QUALITY_COLUMN),
                    source_interaction_ids=source_interaction_ids(row),
                    original_text_ref=original_a.metadata.ref,
                    redacted_text_ref=redacted_a.metadata.ref,
                    redaction_map_ref=map_a.metadata.ref,
                    original_length=len(original),
                    redacted_length=len(redaction.text),
                ),
            ),
            normalization_version=NORMALIZATION_VERSION,
            redaction_version=redaction.map.redaction_version,
        )
        transcript_a = store.put(canonical_json(transcript), media_type=JSON, classification=Classification.INTERNAL)
        artifacts += [original_a, redacted_a, map_a, transcript_a]
        transcript_ref = transcript_a.metadata.ref

    request = IngestionRequest(
        key=key,
        source_record_ref=source.metadata.ref,
        transcript_ref=transcript_ref,
        case_metadata=case_metadata(row),
        evaluation_labels=evaluation_labels(row),
        lane=lane,
        received_at=received_at,
    )
    return PreparedCase(request=request, artifacts=tuple(artifacts))
