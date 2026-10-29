"""Canonical transcript text from V1 ``msg_tx2`` (LDR-0005).

Deliberately minimal so the text stays as close to V1's model input as possible:
Unicode NFC, newlines unified to ``\\n``, control characters (other than ``\\n``
and ``\\t``) dropped, outer whitespace stripped. The result is the "original"
text that every span and redaction offset refers to. Offsets count Unicode code
points (Python ``str`` indices).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .source import InvalidSourceRowError

NORMALIZATION_VERSION = "msg_tx2-norm-1"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_transcript(raw: Any) -> str:
    """Normalized transcript text; ``""`` when the case has none."""
    if raw is None or (isinstance(raw, float) and raw != raw):
        return ""
    if not isinstance(raw, str):
        raise InvalidSourceRowError("TRANSCRIPT_NOT_TEXT")
    text = unicodedata.normalize("NFC", raw).replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", text).strip()
