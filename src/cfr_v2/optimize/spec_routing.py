"""Gold spec for C04 routing (LDR-0027; the router is LDR-0020's).

**Template:** one row per claim with a committed routing result: the claim, the complaints it
rests on, the titles of the procedure sections the router was shown, and where it was routed,
locked; the labeller picks the correct vertical (or UNMAPPED / NO_ELIGIBLE_VERTICAL) from the
current release's definitions.

**Inputs:** the claim and **the same sections**, frozen: rebuilt from the procedure snapshot the
routing searched, so the router is optimized on its own and retrieval stays out of scope.

**Metric:** 1 when the outcome and vertical match the label, else 0; feedback names both.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import dspy

from . import workbook
from .gold import GoldExample, GoldSet, Score

OUTCOMES = ("UNMAPPED", "NO_ELIGIBLE_VERTICAL")
COLUMNS = ("case_no", "claim_no", "claim", "complaints", "sections_shown", "routed_to", "correct", "notes")


@dataclass(frozen=True)
class Label:
    correct: str  # a vertical id, UNMAPPED or NO_ELIGIBLE_VERTICAL


class RoutingSpec:
    prompt_id = "c04.routing"

    def _routed(self, ctx, case_nos: Sequence[str]):
        """case_no -> (claim set, routing result, routing ref); and the cases left out, with why."""
        from cfr_v2.contracts import ClaimSetRevision, RoutingResult

        runs, found, missing = ctx.runs(case_nos), {}, []
        for case_no in case_nos:
            if case_no not in runs:
                missing.append(f"{case_no}: no case run in the state database")
                continue
            routing, ref = ctx.committed(runs[case_no][0], "c04.routing", RoutingResult)
            claim_set, _ = ctx.committed(runs[case_no][0], "c02.pass2", ClaimSetRevision)
            if routing is None or claim_set is None:
                missing.append(f"{case_no}: no committed routing result; run the worker first")
            elif routing.is_stale(claim_set.claim_set_hash):
                missing.append(f"{case_no}: its routing is for an older claim set")
            elif not routing.procedures_ref:
                missing.append(f"{case_no}: its routing names no procedure snapshot")
            else:
                found[case_no] = (claim_set, routing, ref)
        return found, missing

    def template(self, case_ids: Sequence[str], out: Path, ctx) -> list[str]:
        from cfr_v2.routing.router import ClaimInput

        found, missing = self._routed(ctx, case_ids)
        rows = []
        for case_no, (claim_set, routing, _) in found.items():
            inputs = {c.claim_id: c for c in ClaimInput.from_claim_set(claim_set)}
            for i, r in enumerate(routing.claims, start=1):
                claim = inputs.get(r.claim_id)
                if claim is None:
                    continue
                shown = "\n".join(f"S{n}: {s.file_name} / {s.section_title}" for n, s in enumerate(r.sections_seen, 1))
                routed = r.candidates[0].vertical_id if r.candidates else r.outcome.value
                rows.append([case_no, i, claim.statement, "\n".join(claim.complaints), shown, routed])
        choices = [*ctx.catalog.ids, *OUTCOMES]
        workbook.write(
            out,
            title=f"Gold set for C04 routing ({len(rows)} claims in {len(found)} cases)",
            steps=(
                (
                    "1. Each row is one claim: what it says, the complaints it rests on, the procedure sections the router "
                    "was shown, and where it was routed (grey: locked)."
                ),
                (
                    "2. In 'correct', choose the vertical that should review the claim, from the current definitions; or "
                    "UNMAPPED when no vertical's area covers it, or NO_ELIGIBLE_VERTICAL when it alleges no failure in "
                    "American Express's handling."
                ),
                "3. Leave 'correct' empty to leave a claim out.",
            ),
            sheets=(
                workbook.Sheet(
                    "claims",
                    COLUMNS,
                    rows,
                    editable=frozenset({"correct", "notes"}),
                    choices={"correct": choices},
                    wide=frozenset({"claim", "complaints", "sections_shown", "notes"}),
                ),
            ),
            frozen={c: ref for c, (_, _, ref) in found.items()},
        )
        return missing

    def read(self, paths: Sequence[Path], ctx) -> GoldSet:
        from cfr_v2.contracts import ProcedureHit
        from cfr_v2.routing.router import ClaimInput, Section, claim_inputs

        gold, labels, frozen = GoldSet(), {}, {}
        allowed = {*ctx.catalog.ids, *OUTCOMES}
        for path in paths:
            try:
                rows = workbook.read(path, "claims", ("case_no", "claim_no", "correct"))
            except workbook.WorkbookError as exc:
                gold.problems.append(str(exc))
                continue
            refs = workbook.frozen(path)
            for row_no, r in rows:
                if not r["case_no"] or not r["correct"]:
                    continue
                if r["correct"] not in allowed:
                    gold.problems.append(
                        f"{r['case_no']}: claims row {row_no}: {r['correct']!r} isn't a current vertical"
                    )
                    continue
                key = (r["case_no"], r["claim_no"])
                if key in labels and labels[key] != r["correct"]:
                    gold.problems.append(
                        f"{key[0]}: claim {key[1]}: labellers disagree; {path.name}'s answer is not used"
                    )
                labels.setdefault(key, r["correct"])
                frozen.setdefault(r["case_no"], refs.get(r["case_no"], ""))
        found, missing = self._routed(ctx, sorted({c for c, _ in labels}))
        gold.problems += missing
        for case_no, (claim_set, routing, ref) in found.items():
            if frozen.get(case_no) and frozen[case_no] != ref:
                gold.problems.append(f"{case_no}: the routing changed since the template; make a new template")
                continue
            passages = ctx.passages(routing.procedures_ref)
            inputs = {c.claim_id: c for c in ClaimInput.from_claim_set(claim_set)}
            for i, r in enumerate(routing.claims, start=1):
                correct = labels.get((case_no, str(i)))
                if correct is None or r.claim_id not in inputs:
                    continue
                sections = []
                for n, cited in enumerate(r.sections_seen, start=1):
                    p = passages.get((cited.source_sha256, cited.section_index, cited.part))
                    if p is None:
                        break
                    hit = ProcedureHit(
                        procedure_name=p.document.name,
                        file_name=p.document.file_name,
                        source_sha256=p.document.source_sha256,
                        annual_review_date=p.document.annual_review_date,
                        monthly_revision_date=p.document.monthly_revision_date,
                        section_index=p.section.index,
                        section_title=p.section.title,
                        part=p.section.part,
                        page_start=p.section.page_start,
                        page_end=p.section.page_end,
                        text=p.section.text,
                        rank=n,
                    )
                    sections.append(Section(f"S{n}", hit))
                if len(sections) != len(r.sections_seen):
                    gold.problems.append(f"{case_no}: claim {i}: a section it was shown isn't in its snapshot")
                    continue
                example_inputs = claim_inputs(inputs[r.claim_id], sections) | {
                    "section_ids": [s.section_id for s in sections]
                }
                gold.examples.append(GoldExample(case_no, example_inputs, Label(correct)))
        return gold

    def program(self, ctx) -> dspy.Module:
        from cfr_v2.routing.router import Router

        return Router(ctx.catalog)

    def metric(self, label: Label, prediction: dspy.Prediction) -> Score:
        answer = prediction.routing
        outcome = str(getattr(answer, "outcome", "") or "")
        vertical = getattr(answer, "vertical", None)
        got = vertical if outcome == "ROUTED" else outcome
        if got == label.correct:
            return Score(1.0, "Right.")
        return Score(0.0, f"Answered {got or 'nothing'}; the right answer is {label.correct}.")


SPEC = RoutingSpec()

__all__ = ["SPEC", "Label", "RoutingSpec"]
