"""C02 — Complaint Understanding. See lld/components/c02-understanding/README.md.

Two stages: ``c02.pass1`` (allegations, requests, agent statements) and ``c02.pass2``
(claims, grounding, evidence needs). Imports DSPy; the runtime only loads this package in
the worker (``python -m cfr_v2.runtime``).
"""

from __future__ import annotations

from .handler import load_request, load_segments, make_pass1_handler, make_pass2_handler, received_date
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
from .pass2 import PASS2_PROGRAM_ID, PASS2_VERSION, Pass2, build_claim_set, make_signature, program_inputs
from .spans import locate

__all__ = [
    "PASS1_PROGRAM_ID",
    "PASS1_VERSION",
    "PASS2_PROGRAM_ID",
    "PASS2_VERSION",
    "ExtractFromCall",
    "ExtractedAllegation",
    "ExtractedDetail",
    "ExtractedRequest",
    "ExtractedStatement",
    "Pass1",
    "Pass2",
    "build_claim_set",
    "build_pass1_result",
    "load_request",
    "load_segments",
    "locate",
    "make_pass1_handler",
    "make_pass2_handler",
    "make_signature",
    "program_inputs",
    "received_date",
    "transcript_text",
]
