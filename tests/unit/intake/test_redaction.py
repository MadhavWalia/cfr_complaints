from __future__ import annotations

import pytest

from cfr_v2.intake import (
    PII_TOKEN,
    PROFANITY_TOKEN,
    AlignmentError,
    ProfanityFilter,
    RedactionRejectedError,
    align_masked,
    redact,
)
from cfr_v2.intake.fakes import RegexMasker, synthetic_names, synthetic_row
from cfr_v2.intake.normalize import normalize_transcript

T = PII_TOKEN
NO_PROFANITY = ProfanityFilter()


def _mask(original: str, *values: str) -> str:
    for v in values:
        original = original.replace(v, T)
    return original


# -- alignment ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "values"),
    [
        ("call Avery at 555-123-4567 today", ("Avery", "555-123-4567")),
        ("Avery called", ("Avery",)),  # mask at start
        ("this is Avery", ("Avery",)),  # mask at end
        ("Avery", ("Avery",)),  # whole text
        ("no pii here", ()),
        ("Avery and Avery again", ("Avery",)),  # repeated value
    ],
)
def test_alignment_recovers_masked_ranges(original, values):
    spans = align_masked(original, _mask(original, *values), T)
    assert [original[s:e] for s, e in spans] == [v for v in sorted_occurrences(original, values)]


def sorted_occurrences(text: str, values: tuple[str, ...]) -> list[str]:
    hits = []
    for v in values:
        start = 0
        while (i := text.find(v, start)) >= 0:
            hits.append((i, v))
            start = i + len(v)
    return [v for _, v in sorted(hits)]


def test_adjacent_masks_merge_into_one_range():
    original = "name: AveryLindqvist."
    masked = f"name: {T}{T}."
    assert align_masked(original, masked, T) == [(6, 20)]


@pytest.mark.parametrize(
    "masked",
    [
        f"CALL {T} today",  # text before first mask changed
        f"call {T} tomorrow",  # text after last mask changed
        f"call {T} at {T} now please",  # middle text changed
        "call Avery at 555-123-4567 today!",  # changed without a mask
    ],
)
def test_alignment_rejects_changed_text(masked):
    with pytest.raises(AlignmentError):
        align_masked("call Avery at 555-123-4567 today", masked, T)


def test_ambiguous_alignment_shifts_boundaries_but_never_reveals_text():
    # The masker took "A at B", and the next unchanged piece " at B " also occurs
    # inside that value, so leftmost matching puts the boundary early (LDR-0010).
    original = "hi A at B at B 555-123-4567"
    masked = f"hi {T} at B {T}"
    spans = align_masked(original, masked, T)
    assert spans == [(3, 4), (10, len(original))]
    # Rendering from these spans reproduces the masker's output exactly.
    rendered = original[:3] + T + original[4:10] + T
    assert rendered == masked


# -- redact --------------------------------------------------------------------------


def test_spans_translate_back_to_original_values():
    row = synthetic_row(3)
    original = normalize_transcript(row["msg_tx2"])
    out = redact(original, interaction_id="msg_tx2", masker=RegexMasker(synthetic_names()), profanity=NO_PROFANITY)

    assert "@example.com" not in out.text
    assert not any(name in out.text for name in synthetic_names())
    for span in out.map.spans:
        assert out.text[span.redacted_start : span.redacted_end] == T
        start, end = out.map.to_original(span.redacted_start, span.redacted_end)
        assert (start, end) == (span.original_start, span.original_end)
    # An unredacted range maps back to identical text.
    first = out.map.spans[0]
    tail_r = (first.redacted_end, first.redacted_end + 20)
    tail_o = out.map.to_original(*tail_r)
    assert original[slice(*tail_o)] == out.text[slice(*tail_r)]


def test_profanity_and_pii_share_one_map():
    original = "Customer: darn it, my email is a.b@example.com and this darn fee"
    out = redact(original, interaction_id="t", masker=RegexMasker(), profanity=ProfanityFilter(["darn"]))
    assert out.text == f"Customer: {PROFANITY_TOKEN} it, my email is {T} and this {PROFANITY_TOKEN} fee"
    assert [s.entity_type for s in out.map.spans] == ["PROFANITY", "PII", "PROFANITY"]


def test_overlapping_profanity_inside_pii_becomes_one_pii_span():
    original = "email darn.it@example.com now"
    out = redact(original, interaction_id="t", masker=RegexMasker(), profanity=ProfanityFilter(["darn"]))
    assert out.text == f"email {T} now"
    assert [s.entity_type for s in out.map.spans] == ["PII"]


def test_profanity_is_whole_word_and_case_insensitive():
    spans = ProfanityFilter(["darn"]).spans("Darn, darning DARN")
    assert spans == [(0, 4), (14, 18)]


def test_missing_word_list_masks_nothing(tmp_path):
    assert ProfanityFilter.from_file(tmp_path / "absent.txt").spans("anything at all") == []


def test_sentinel_in_source_text_is_rejected():
    with pytest.raises(RedactionRejectedError) as exc:
        redact(f"weird {T} text", interaction_id="t", masker=RegexMasker(), profanity=NO_PROFANITY)
    assert exc.value.reason == "SENTINEL_COLLISION"


class _Exploding:
    version = "boom"

    def mask(self, text: str) -> str:
        raise RuntimeError(f"analyzer failed on: {text}")


class _Rewriting:
    version = "rewrite"

    def mask(self, text: str) -> str:
        return text.upper().replace("AVERY", T)


def test_masker_failure_reason_never_contains_text():
    with pytest.raises(RedactionRejectedError) as exc:
        redact("secret Avery", interaction_id="t", masker=_Exploding(), profanity=NO_PROFANITY)
    assert exc.value.reason == "REDACTION_FAILED:RuntimeError"
    assert exc.value.__cause__ is None and "Avery" not in str(exc.value)


def test_masker_that_changes_other_text_fails_alignment():
    with pytest.raises(RedactionRejectedError) as exc:
        redact("hello Avery", interaction_id="t", masker=_Rewriting(), profanity=NO_PROFANITY)
    assert exc.value.reason == "REDACTION_ALIGNMENT_FAILED"


def test_redaction_is_deterministic():
    original = normalize_transcript(synthetic_row(7)["msg_tx2"])
    masker = RegexMasker(synthetic_names())
    a = redact(original, interaction_id="t", masker=masker, profanity=NO_PROFANITY)
    b = redact(original, interaction_id="t", masker=masker, profanity=NO_PROFANITY)
    assert a == b
    assert a.map.redaction_version == "sentinel-mask-1+regex-fake-1"
