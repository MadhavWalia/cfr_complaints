from __future__ import annotations

import pytest

from cfr_v2.intake import InvalidSourceRowError, normalize_transcript


def test_newlines_controls_and_outer_whitespace():
    assert normalize_transcript("  a\r\nb\rc\x00d\te\x1f  ") == "a\nb\ncd\te"


def test_unicode_nfc():
    assert normalize_transcript("café") == "café"


@pytest.mark.parametrize("raw", [None, float("nan"), "", "   "])
def test_missing_transcript_is_empty(raw):
    assert normalize_transcript(raw) == ""


def test_non_text_rejected():
    with pytest.raises(InvalidSourceRowError) as exc:
        normalize_transcript(42)
    assert exc.value.reason == "TRANSCRIPT_NOT_TEXT"


def test_v1_separators_kept():
    # msg_tx2 joins calls with "|"; normalization keeps V1's text as is.
    assert normalize_transcript("call one|call two") == "call one|call two"
