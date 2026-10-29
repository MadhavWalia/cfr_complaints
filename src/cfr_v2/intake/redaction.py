"""Redaction with derived offsets (LDR-0004, LDR-0010).

``ee_utils.redaction.redact_text`` returns no positions, so it runs in masking
mode with a sentinel token and the masked ranges are recovered by aligning the
unchanged pieces against the original. Profanity matches come straight from a
regex on the original. Both span sets are merged and rendered by us, so all
offset changes live in one ``RedactionMap``.

Exceptions never carry transcript text: library errors are reduced to their type.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cfr_v2.contracts import RedactionMap, RedactionSpan, sha256_hex
from cfr_v2.telemetry import get_logger

from .source import InvalidSourceRowError

logger = get_logger(__name__)

PII_TOKEN = "⟦PII⟧"
PROFANITY_TOKEN = "⟦PROFANITY⟧"
REDACTION_VERSION = "sentinel-mask-1"
PII, PROFANITY = "PII", "PROFANITY"
_TOKENS = {PII: PII_TOKEN, PROFANITY: PROFANITY_TOKEN}


class RedactionRejectedError(InvalidSourceRowError):
    pass


class AlignmentError(ValueError):
    pass


class Masker(Protocol):
    """Replaces every detected PII value in ``text`` with ``PII_TOKEN``."""

    version: str

    def mask(self, text: str) -> str: ...


class EEUtilsMasker:
    """``ee_utils.redaction`` in masking mode; other settings from ``config_gpt.yml`` ``redaction:``."""

    version = "ee-utils-masking"

    def __init__(self, config: Any = None) -> None:
        from ee_utils.redaction import MaskingConfig, redact_text

        if config is None:
            from ee_config import load_config

            config = load_config().redaction
        self._config = config.model_copy(
            update={"mode": "masking", "masking": MaskingConfig(replacement_text=PII_TOKEN)}
        )
        self._redact_text = redact_text

    def mask(self, text: str) -> str:
        return self._redact_text(text, self._config).text


class ProfanityFilter:
    """V1 word-list filter (``toxic_words.txt``): whole words, case-insensitive."""

    def __init__(self, words: Iterable[str] = ()) -> None:
        cleaned = sorted({w.strip() for w in words if w.strip()}, key=len, reverse=True)
        self._pattern = (
            re.compile(r"\b(" + "|".join(re.escape(w) for w in cleaned) + r")\b", re.IGNORECASE) if cleaned else None
        )

    @classmethod
    def from_file(cls, path: Path | None) -> "ProfanityFilter":
        if path is None or not path.is_file():
            # Same as V1: without a word list the filter matches nothing.
            logger.warning("profanity_filter: no word list at %s; profanity is not masked", path)
            return cls()
        return cls(path.read_text(encoding="utf-8").splitlines())

    def spans(self, text: str) -> list[tuple[int, int]]:
        return [] if self._pattern is None else [m.span() for m in self._pattern.finditer(text)]


@dataclass(frozen=True)
class Redaction:
    text: str
    map: RedactionMap


def align_masked(original: str, masked: str, token: str) -> list[tuple[int, int]]:
    """Original ``[start, end)`` ranges that ``masked`` replaced with ``token``.

    The pieces between tokens must appear unchanged and in order. Each is matched
    at its leftmost position after the previous one; adjacent tokens merge into one
    range, since their boundary is unknowable.
    """
    pieces = masked.split(token)
    if len(pieces) == 1:
        if masked != original:
            raise AlignmentError("text changed without a mask token")
        return []
    if not original.startswith(pieces[0]):
        raise AlignmentError("text before the first mask changed")
    spans: list[tuple[int, int]] = []
    span_start = len(pieces[0])
    for i, piece in enumerate(pieces[1:], start=1):
        last = i == len(pieces) - 1
        if not piece and not last:
            continue  # adjacent masks
        if last:
            idx = len(original) - len(piece)
            if not original.endswith(piece) or idx <= span_start:
                raise AlignmentError("text after the last mask changed")
        else:
            idx = original.find(piece, span_start + 1)
            if idx < 0:
                raise AlignmentError("text between masks changed")
        spans.append((span_start, idx))
        span_start = idx + len(piece)
    return spans


def _merge(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in sorted(spans):
        if merged and start < merged[-1][1]:
            p_start, p_end, p_kind = merged[-1]
            merged[-1] = (p_start, max(p_end, end), PII if PII in (p_kind, kind) else p_kind)
        else:
            merged.append((start, end, kind))
    return merged


def redact(original: str, *, interaction_id: str, masker: Masker, profanity: ProfanityFilter) -> Redaction:
    if any(token in original for token in _TOKENS.values()):
        raise RedactionRejectedError("SENTINEL_COLLISION")
    try:
        masked = masker.mask(original) if original else original
    except Exception as exc:  # library errors may embed text; keep the type only
        raise RedactionRejectedError(f"REDACTION_FAILED:{type(exc).__name__}") from None
    try:
        pii = align_masked(original, masked, PII_TOKEN)
    except AlignmentError:
        raise RedactionRejectedError("REDACTION_ALIGNMENT_FAILED") from None

    parts: list[str] = []
    spans: list[RedactionSpan] = []
    pos = out_len = 0
    for start, end, kind in _merge([(s, e, PII) for s, e in pii] + [(s, e, PROFANITY) for s, e in profanity.spans(original)]):
        parts.append(original[pos:start])
        out_len += start - pos
        token = _TOKENS[kind]
        spans.append(
            RedactionSpan(
                original_start=start,
                original_end=end,
                redacted_start=out_len,
                redacted_end=out_len + len(token),
                entity_type=kind,
            )
        )
        parts.append(token)
        out_len += len(token)
        pos = end
    parts.append(original[pos:])
    text = "".join(parts)
    return Redaction(
        text=text,
        map=RedactionMap(
            interaction_id=interaction_id,
            original_sha256=sha256_hex(original.encode("utf-8")),
            redacted_sha256=sha256_hex(text.encode("utf-8")),
            spans=tuple(spans),
            redaction_version=f"{REDACTION_VERSION}+{masker.version}",
        ),
    )
