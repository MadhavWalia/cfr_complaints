"""C03 — Primary claim selector. See lld/components/c03-primary-selector/README.md and LDR-0018.

Imports DSPy; the runtime only loads this package in the worker (``python -m cfr_v2.runtime``).
"""

from __future__ import annotations

from .handler import load_claim_set, make_selection_handler
from .selector import (
    INSTRUCTIONS,
    SELECT_PROGRAM_ID,
    SELECT_VERSION,
    Selector,
    build_selection,
    make_signature,
    selector_inputs,
    shortcut,
    supporting_spans,
)

__all__ = [
    "INSTRUCTIONS",
    "SELECT_PROGRAM_ID",
    "SELECT_VERSION",
    "Selector",
    "build_selection",
    "load_claim_set",
    "make_selection_handler",
    "make_signature",
    "selector_inputs",
    "shortcut",
    "supporting_spans",
]
