from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.contracts import (
    AuthorizationContext,
    CanonicalTranscript,
    Classification,
    Lane,
    RedactionMap,
)
from cfr_v2.intake import (
    INGESTION_REVISION,
    PII_TOKEN,
    ProfanityFilter,
    RedactionRejectedError,
    prepare_case,
)
from cfr_v2.intake.fakes import RegexMasker, synthetic_names, synthetic_row

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
READ_ALL = AuthorizationContext(
    principal="test", purpose="test", allowed_classifications=frozenset({"RESTRICTED", "INTERNAL"})
)
UNUSED = {"potl_defect_flag", "err_type", "err_sub_type", "cm11", "cm15", "case_note", "latest_rslv_note", "creat_opr_id"}


@pytest.fixture
def store():
    return InMemoryArtifactStore()


def _prepare(store, row, received_at=NOW):
    return prepare_case(
        row,
        store=store,
        masker=RegexMasker(synthetic_names()),
        profanity=ProfanityFilter(),
        lane=Lane.BATCH,
        received_at=received_at,
    )


def _get(store, ref, classification):
    return store.get(ref, classification=classification, authorization=READ_ALL)


def test_request_carries_only_v1_fields(store):
    row = synthetic_row(0)
    req = _prepare(store, row).request
    assert req.key.source_case_id == row["case_no"]
    assert req.key.ingestion_revision == INGESTION_REVISION == 0
    assert req.case_metadata == {"prim_ctgy": row["prim_ctgy"], "prim_subctgy": row["prim_subctgy"]}
    assert req.evaluation_labels == {"latest_rslv_optn": row["latest_rslv_optn"]}
    dumped = req.model_dump_json()
    assert not any(f'"{f}"' in dumped for f in UNUSED)
    assert row["msg_tx2"][:30] not in dumped


def test_artifacts_and_classifications(store):
    row = synthetic_row(1)
    prepared = _prepare(store, row)
    assert prepared.has_transcript
    by_class = {a.metadata.ref: a.metadata.classification for a in prepared.artifacts}
    assert by_class[prepared.request.source_record_ref] is Classification.RESTRICTED
    assert json.loads(_get(store, prepared.request.source_record_ref, Classification.RESTRICTED))["cm15"] == row["cm15"]

    transcript = CanonicalTranscript.model_validate_json(_get(store, prepared.request.transcript_ref, Classification.INTERNAL))
    (interaction,) = transcript.interactions
    assert by_class[interaction.original_text_ref] is Classification.RESTRICTED
    assert by_class[interaction.redaction_map_ref] is Classification.RESTRICTED
    assert by_class[interaction.redacted_text_ref] is Classification.INTERNAL
    assert interaction.linkage_quality == row["exact_match_flag"]
    assert interaction.source_interaction_ids == tuple(sorted(row["intrct_ids"]))

    redacted = _get(store, interaction.redacted_text_ref, Classification.INTERNAL).decode()
    assert PII_TOKEN in redacted and "@example.com" not in redacted
    rmap = RedactionMap.model_validate_json(_get(store, interaction.redaction_map_ref, Classification.RESTRICTED))
    original = _get(store, interaction.original_text_ref, Classification.RESTRICTED).decode()
    assert len(original) == interaction.original_length and len(redacted) == interaction.redacted_length
    span = rmap.spans[0]
    assert original[span.original_start : span.original_end] in original
    assert redacted[span.redacted_start : span.redacted_end] == PII_TOKEN


def test_preparation_is_idempotent_apart_from_arrival_time(store):
    row = synthetic_row(2)
    a = _prepare(store, row)
    b = _prepare(store, row, received_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert [x.metadata.ref for x in a.artifacts] == [x.metadata.ref for x in b.artifacts]
    assert a.request.content_hash() == b.request.content_hash()
    assert a.request != b.request


@pytest.mark.parametrize("msg", [None, "", "   "])
def test_case_without_transcript_is_still_prepared(store, msg):
    prepared = _prepare(store, synthetic_row(3, msg_tx2=msg))
    assert not prepared.has_transcript
    assert [a.metadata.classification for a in prepared.artifacts] == [Classification.RESTRICTED]


def test_redaction_failure_stores_nothing(store):
    with pytest.raises(RedactionRejectedError):
        _prepare(store, synthetic_row(4, msg_tx2=f"contains {PII_TOKEN} already"))
    assert store.objects == {}
