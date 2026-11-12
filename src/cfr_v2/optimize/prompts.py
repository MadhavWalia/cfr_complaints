"""The pipeline's prompts, and what optimizing one needs (LDR-0027).

Each prompt is one DSPy program with one predictor whose instructions an optimizer may rewrite.
Where those instructions live in the source (a signature's docstring or an ``INSTRUCTIONS``
constant) and which constant carries the program version tell ``report.patch`` how to write a
candidate back as a reviewable change.

A prompt can be optimized once it has a **gold spec** (``gold.GoldSpec``): its template, reader,
checks, program and metric. Phase 1 builds the machinery; the specs arrive in phase 2.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]  # src/cfr_v2


@dataclass(frozen=True)
class Prompt:
    prompt_id: str  # the stage's logical task id
    title: str
    does: str  # one line: what the prompt produces, from what
    module: str  # where the program lives
    predictor: str  # the program's predictor holding the editable instructions
    instructions: str  # "INSTRUCTIONS" (a constant) or "doc:<Signature class>" (its docstring)
    version_constant: str
    source: Path | None = None  # the module's file, when it isn't under src/cfr_v2 (tests)

    @property
    def path(self) -> Path:
        return self.source or SRC / Path(*self.module.split(".")[1:]).with_suffix(".py")

    def current_version(self) -> str:
        return getattr(importlib.import_module(self.module), self.version_constant)

    def current_instructions(self) -> str:
        module = importlib.import_module(self.module)
        if self.instructions.startswith("doc:"):
            return getattr(module, self.instructions[4:]).instructions
        return getattr(module, self.instructions).strip()


PROMPTS: tuple[Prompt, ...] = (
    Prompt(
        "c02.pass1",
        "C02 Pass 1",
        "Allegations, requests and agent statements, with quotes, from the redacted transcript.",
        "cfr_v2.understanding.pass1",
        "extract",
        "doc:ExtractFromCall",
        "PASS1_VERSION",
    ),
    Prompt(
        "c02.pass2",
        "C02 Pass 2",
        "Claims, accounts, event windows and evidence needs, from Pass 1's items.",
        "cfr_v2.understanding.pass2",
        "ground",
        "INSTRUCTIONS",
        "PASS2_VERSION",
    ),
    Prompt(
        "c03.primary_selection",
        "C03 selection",
        "The primary claim, or AMBIGUOUS, from the claims and requests.",
        "cfr_v2.selection.selector",
        "select",
        "INSTRUCTIONS",
        "SELECT_VERSION",
    ),
    Prompt(
        "c04.routing",
        "C04 routing",
        "One vertical per claim, from the claim, procedure sections and vertical definitions.",
        "cfr_v2.routing.router",
        "route",
        "INSTRUCTIONS",
        "ROUTE_VERSION",
    ),
)
BY_ID: dict[str, Prompt] = {p.prompt_id: p for p in PROMPTS}

__all__ = ["BY_ID", "PROMPTS", "Prompt"]
