"""C02 — Complaint Understanding. See lld/components/c02-understanding/README.md.

Imports DSPy; the runtime only loads this package in the worker (``python -m cfr_v2.runtime``).
"""

from __future__ import annotations

from .handler import load_segments, make_understanding_handler
from .pass1 import (
    PASS1_PROGRAM_ID,
    PASS1_VERSION,
    ExtractedAllegation,
    ExtractedDetail,
    ExtractedRequest,
    ExtractedStatement,
    ExtractFromCall,
    Pass1,
    build_pass1_result,
    transcript_text,
)
from .spans import locate

__all__ = [
    "PASS1_PROGRAM_ID",
    "PASS1_VERSION",
    "ExtractFromCall",
    "ExtractedAllegation",
    "ExtractedDetail",
    "ExtractedRequest",
    "ExtractedStatement",
    "Pass1",
    "build_pass1_result",
    "load_segments",
    "locate",
    "make_understanding_handler",
    "transcript_text",
]
