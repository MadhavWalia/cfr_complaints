"""Deterministic quote matching for Pass 1 (C02 LLD, span fidelity).

The model quotes the redacted transcript; code, not the model, turns each quote into
offsets. A quote matches when its words occur in order in one interaction's redacted
text, with any run of whitespace between them. Typographic quote marks match their
plain forms. An exact-case match is preferred; a case-insensitive one is accepted.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from cfr_v2.contracts import SpanRef

_SINGLE = "'‘’ʼ`"
_DOUBLE = '"“”'
# Wrapping the model sometimes adds around a quote: quote marks, ellipses, spaces.
_WRAPPING = _SINGLE + _DOUBLE + "…. \t\r\n"


def _char(c: str) -> str:
    if c in _SINGLE:
        return f"[{re.escape(_SINGLE)}]"
    if c in _DOUBLE:
        return f"[{re.escape(_DOUBLE)}]"
    return re.escape(c)


def quote_pattern(quote: str) -> re.Pattern[str] | None:
    words = quote.strip(_WRAPPING).split()
    if not words:
        return None
    return re.compile(r"\s+".join("".join(_char(c) for c in w) for w in words))


def locate(quote: str, segments: Sequence[tuple[str, str]]) -> SpanRef | None:
    """First occurrence of ``quote`` in ``segments`` (``(interaction_id, redacted_text)``), or None."""
    pattern = quote_pattern(quote)
    if pattern is None:
        return None
    for flags in (0, re.IGNORECASE):
        compiled = re.compile(pattern.pattern, flags)
        for interaction_id, text in segments:
            if m := compiled.search(text):
                return SpanRef(interaction_id=interaction_id, start=m.start(), end=m.end())
    return None
