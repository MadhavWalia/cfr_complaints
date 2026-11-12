"""Prompt optimization with DSPy against gold sets (LDR-0027). See ``__main__`` for the commands."""

from .gold import GoldExample, GoldSet, GoldSpec, Score, Split, split
from .prompts import BY_ID, PROMPTS, Prompt
from .report import Report, patch

__all__ = [
    "BY_ID",
    "PROMPTS",
    "GoldExample",
    "GoldSet",
    "GoldSpec",
    "Prompt",
    "Report",
    "Score",
    "Split",
    "patch",
    "split",
]
