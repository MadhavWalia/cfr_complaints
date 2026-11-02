"""C02 — Complaint Understanding. See lld/components/c02-understanding/README.md.

Imports DSPy; the runtime only loads this package in the worker (``python -m cfr_v2.runtime``).
"""

from __future__ import annotations

from .handler import load_segments, make_understanding_handler
from .pass1 import (
    PASS1_PROGRAM_ID,
    PASS1_VERSION,
    ExtractAllegations,
    ExtractedAllegation,
    Pass1,
    build_allegation_set,
    transcript_text,
)
from .spans import locate

__all__ = [
    "PASS1_PROGRAM_ID",
    "PASS1_VERSION",
    "ExtractAllegations",
    "ExtractedAllegation",
    "Pass1",
    "build_allegation_set",
    "load_segments",
    "locate",
    "make_understanding_handler",
    "transcript_text",
]
