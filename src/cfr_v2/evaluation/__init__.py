"""Evaluation by stage (LDR-0042): export a stage's results for review, read the reviewed workbook
back, and compute the stage's metrics.

    python -m cfr_v2.evaluation export routing --release cfr-v2-phase-e --out routing_review.xlsx
    python -m cfr_v2.evaluation score routing routing_review.xlsx --out-dir metrics/

The stages are Intake, Pass 1, Pass 2, Selection and Routing. Planning (C05) writes placeholder
plans today, so it has nothing to review yet.
"""

from __future__ import annotations

from .intake import Intake
from .pass1 import Pass1
from .pass2 import Pass2
from .routing import Routing
from .selection import Selection
from .stage import Stage

STAGES: dict[str, Stage] = {s.key: s for s in (Intake(), Pass1(), Pass2(), Selection(), Routing())}

__all__ = ["STAGES", "Stage"]
