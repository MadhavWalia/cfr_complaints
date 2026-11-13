"""Gold spec for C02 Pass 1 (LDR-0027): the existing gold workbook, reader and scorer.

Input: the case's redacted transcript, as the stage sends it. Label: the labeller's items with
their quotes located in that transcript (``scoring.gold_side``; a quote with redacted text in it
is found in the original and mapped across). Metric: the predicted items placed in the same
transcript (``build_pass1_result``) and matched to the gold ones by quote overlap, as
``score run`` does; the score is the mean F1 over allegations, requests and agent statements.
Feedback holds counts and types only: gold quotes can hold unredacted text and never reach a model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import dspy

from .gold import GoldExample, GoldSet, Score

MIN_OVERLAP = 0.3
LISTS = (("allegations", "allegations"), ("requests", "requests"), ("statements", "agent statements"))


@dataclass(frozen=True)
class Label:
    case_no: str
    side: object  # scoring.Side: the gold items with located spans
    segments: tuple[tuple[str, str], ...]  # the redacted transcript the spans index
    transcript_ref: str


class Pass1Spec:
    prompt_id = "c02.pass1"

    def template(self, case_ids: Sequence[str], out: Path, ctx) -> list[str]:
        from cfr_v2.understanding.gold import write_template

        write_template(list(case_ids), out)
        return []

    def read(self, paths: Sequence[Path], ctx) -> GoldSet:
        from cfr_v2.understanding.gold import GoldError, read_gold
        from cfr_v2.understanding.pass1 import transcript_text
        from cfr_v2.understanding.score import case_text
        from cfr_v2.understanding.scoring import gold_side

        gold = GoldSet()
        cases = {}
        for path in paths:
            try:
                for case in read_gold(path):
                    cases.setdefault(case.case_no, case)  # the first workbook given wins for a shared case
            except GoldError as exc:
                gold.problems.append(str(exc))
        labelled = {c: v for c, v in cases.items() if not v.could_not_label}
        runs = ctx.runs(sorted(labelled))
        for case_no, case in sorted(labelled.items()):
            if case_no not in runs:
                gold.problems.append(f"{case_no}: no case run in the state database; admit it first")
                continue
            request = ctx.request(runs[case_no][1])
            if request.transcript_ref is None:
                gold.problems.append(f"{case_no}: the case has no transcript")
                continue
            text = case_text(ctx.store, request.transcript_ref)
            side, problems = gold_side(case, text)
            gold.problems += [f"{p.case_no}: {p.source} sheet {p.sheet} row {p.row}: quote not found" for p in problems]
            label = Label(case_no, side, tuple(text.segments), request.transcript_ref)
            gold.examples.append(GoldExample(case_no, {"transcript": transcript_text(text.segments)}, label))
        return gold

    def program(self, ctx) -> dspy.Module:
        from cfr_v2.understanding.pass1 import Pass1

        return Pass1()

    def metric(self, label: Label, prediction: dspy.Prediction) -> Score:
        from cfr_v2.understanding.pass1 import build_pass1_result
        from cfr_v2.understanding.scoring import pipeline_side, score_sides

        result = build_pass1_result(
            prediction.allegations or [],
            prediction.requests or [],
            prediction.agent_statements or [],
            label.segments,
            case_id=label.case_no,
            transcript_ref=label.transcript_ref,
        )
        scores = score_sides(label.side, pipeline_side(result), MIN_OVERLAP)
        f1s, notes = [], []
        for key, name in LISTS:
            s = scores[key]
            # From the counts: the scorer's f1 is None when one side is empty, which must score 0, not 1
            f1s.append(1.0 if s.gold == s.predicted == 0 else 2 * s.matched / (s.gold + s.predicted))
            if s.matched < s.gold or s.predicted > s.matched:
                notes.append(f"{name}: found {s.matched} of {s.gold}, {s.predicted - s.matched} not in the gold set")
            if s.matched and s.same_kind < s.matched:
                notes.append(f"{name}: wrong type on {s.matched - s.same_kind} of {s.matched} found")
        if result.dropped:
            notes.append(f"{len(result.dropped)} items dropped: their quotes weren't exact copies of the transcript")
        return Score(sum(f1s) / len(f1s), "; ".join(notes) or "All items found with the right types.")


SPEC = Pass1Spec()

__all__ = ["SPEC", "Label", "Pass1Spec"]
