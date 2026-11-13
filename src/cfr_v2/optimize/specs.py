"""The gold specs, one per prompt (LDR-0027)."""

from __future__ import annotations

from .gold import GoldSpec
from .spec_pass1 import SPEC as PASS1
from .spec_pass2 import SPEC as PASS2
from .spec_routing import SPEC as ROUTING
from .spec_selection import SPEC as SELECTION

SPECS: dict[str, GoldSpec] = {spec.prompt_id: spec for spec in (PASS1, PASS2, SELECTION, ROUTING)}

__all__ = ["SPECS"]
