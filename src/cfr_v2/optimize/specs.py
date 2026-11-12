"""The gold specs, one per prompt that can be optimized (LDR-0027). Phase 2 adds them."""

from __future__ import annotations

from .gold import GoldSpec

SPECS: dict[str, GoldSpec] = {}

__all__ = ["SPECS"]
