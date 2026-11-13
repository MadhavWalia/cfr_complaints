"""Gold sets: what a prompt's labelled examples look like, and how they are split (LDR-0027).

A ``GoldSpec`` is everything one prompt needs: a template workbook to fill, a reader that turns
filled workbooks into examples (and lists what is wrong with them), the program to optimize, and
a metric that scores one prediction against its label with a sentence of feedback. Inputs are
always built from redacted artifacts or committed stage results, never from raw transcripts.

The split is by case, from a hash of the case and the seed, so it doesn't depend on the order of
the workbooks and a case never sits on both sides. The optimizer sees train and validation; test
is held out for the report.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import dspy

MIN_EXAMPLES = 20  # fewer: an optimizer only learns the examples
WARN_EXAMPLES = 50  # fewer: a test split too small to trust a difference of a few points
SPLIT = (0.5, 0.25, 0.25)  # train, validation, test


@dataclass(frozen=True)
class GoldExample:
    case_id: str  # the case (or claim) the example belongs to: the unit of the split
    inputs: Mapping[str, Any]  # the program's inputs, exactly as it receives them
    label: Any  # whatever the spec's metric compares the prediction with

    def to_dspy(self) -> dspy.Example:
        return dspy.Example(case_id=self.case_id, gold=self.label, **self.inputs).with_inputs(*self.inputs)


@dataclass
class GoldSet:
    examples: list[GoldExample] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # what the labeller should fix; ids and counts only


@dataclass(frozen=True)
class Score:
    score: float  # 0 to 1
    feedback: str  # what was wrong, for GEPA's reflection; no case text beyond the program's own output


class GoldSpec(Protocol):
    prompt_id: str

    def template(self, case_ids: Sequence[str], out: Path, ctx: Any) -> list[str]:
        """Write the template workbook for ``case_ids``; return the ids it couldn't include, with why."""

    def read(self, paths: Sequence[Path], ctx: Any) -> GoldSet: ...

    def program(self, ctx: Any) -> dspy.Module: ...

    def metric(self, label: Any, prediction: dspy.Prediction) -> Score: ...


@dataclass(frozen=True)
class Split:
    train: list[GoldExample]
    val: list[GoldExample]
    test: list[GoldExample]
    seed: int

    @property
    def sizes(self) -> tuple[int, int, int]:
        return len(self.train), len(self.val), len(self.test)


class NotEnoughExamples(ValueError):
    pass


def split(examples: Sequence[GoldExample], seed: int) -> Split:
    """Deterministic by case and seed; refuses fewer than ``MIN_EXAMPLES`` examples."""
    if len(examples) < MIN_EXAMPLES:
        raise NotEnoughExamples(
            f"{len(examples)} usable examples; at least {MIN_EXAMPLES} are needed to optimize "
            f"(and {WARN_EXAMPLES} to trust a difference of a few points)"
        )
    cases = sorted({e.case_id for e in examples}, key=lambda c: hashlib.sha256(f"{seed}:{c}".encode()).hexdigest())
    n_train = round(len(cases) * SPLIT[0])
    n_val = round(len(cases) * SPLIT[1])
    side = {c: 0 if i < n_train else 1 if i < n_train + n_val else 2 for i, c in enumerate(cases)}
    parts: tuple[list[GoldExample], ...] = ([], [], [])
    for example in examples:
        parts[side[example.case_id]].append(example)
    return Split(*parts, seed=seed)


def warnings(examples: Sequence[GoldExample]) -> list[str]:
    if len(examples) < WARN_EXAMPLES:
        return [f"only {len(examples)} examples: a difference of a few points on the test split may be noise"]
    return []


def dataset_hash(examples: Sequence[GoldExample]) -> str:
    """Identifies the data a report was made from, without carrying any of it."""
    rows = sorted(
        json.dumps({"case": e.case_id, "inputs": dict(e.inputs), "label": e.label}, sort_keys=True, default=str)
        for e in examples
    )
    text = "\n".join(rows)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


__all__ = [
    "MIN_EXAMPLES",
    "WARN_EXAMPLES",
    "GoldExample",
    "GoldSet",
    "GoldSpec",
    "NotEnoughExamples",
    "Score",
    "Split",
    "dataset_hash",
    "split",
    "warnings",
]
