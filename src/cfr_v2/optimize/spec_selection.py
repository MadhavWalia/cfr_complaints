"""Gold spec for C03 primary selection (LDR-0027; the definition is LDR-0018's).

**Template:** per case, its committed claims numbered in the order the customer raised them (the
order the selector sees), with what the customer asked for, on locked sheets; the labeller gives
the main complaint's claim number on ``cases``, or marks the case unclear when two or more claims
are equally central. Cases with fewer than two claims are left out: the stage answers them
without the model.

**Inputs:** the claims and requests exactly as the stage sends them (``selector_inputs``), from
the committed claim set named in the template.

**Metric:** a clear case scores 1 when the selector picks the labelled claim, 0.25 when it answers
AMBIGUOUS with that claim among its candidates, else 0; an unclear case scores 1 for AMBIGUOUS.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import dspy

from . import workbook
from .gold import GoldExample, GoldSet, Score

CASE_COLUMNS = ("case_no", "labeller", "primary_claim_no", "unclear", "could_not_label", "notes")


@dataclass(frozen=True)
class Label:
    primary: str | None  # the claim id; None when unclear
    unclear: bool
    numbers: tuple[str, ...]  # claim ids in the order shown, for feedback by number


class SelectionSpec:
    prompt_id = "c03.primary_selection"

    def _claim_sets(self, ctx, case_nos: Sequence[str]):
        from cfr_v2.contracts import ClaimSetRevision

        runs, found, missing = ctx.runs(case_nos), {}, []
        for case_no in case_nos:
            if case_no not in runs:
                missing.append(f"{case_no}: no case run in the state database")
                continue
            claim_set, ref = ctx.committed(runs[case_no][0], "c02.pass2", ClaimSetRevision)
            if claim_set is None:
                missing.append(f"{case_no}: no committed claim set; run Pass 2 first")
            elif len(claim_set.claims) < 2:
                missing.append(f"{case_no}: {len(claim_set.claims)} claim(s): C03 answers it without the model")
            else:
                found[case_no] = (claim_set, ref)
        return found, missing

    def template(self, case_ids: Sequence[str], out: Path, ctx) -> list[str]:
        from cfr_v2.selection.selector import claim_order

        found, missing = self._claim_sets(ctx, case_ids)
        claims, requests = [], []
        for case_no, (claim_set, _) in found.items():
            ordered = claim_order(claim_set)
            number = {c.claim_id: i for i, c in enumerate(ordered, start=1)}
            claim_of = {a: c.claim_id for c in claim_set.claims for a in c.allegation_ids}
            for i, c in enumerate(ordered, start=1):
                claims.append([case_no, i, c.statement])
            for r in claim_set.requests:
                linked = sorted({number[claim_of[a]] for a in r.allegation_ids if a in claim_of})
                requests.append([case_no, r.text, r.request_type.value, ", ".join(map(str, linked))])
        workbook.write(
            out,
            title=f"Gold set for C03: the main complaint ({len(found)} cases)",
            steps=(
                (
                    "1. For each case, read its claims (numbered in the order the customer raised them) and what the "
                    "customer asked for (grey sheets, locked)."
                ),
                (
                    "2. The main complaint is the main reason the customer contacted American Express: what they would "
                    "answer if asked 'what is this complaint about?'. Not necessarily the first or the most serious."
                ),
                (
                    "3. On 'cases', write its claim number in primary_claim_no. When two or more claims are equally "
                    "central, leave it empty and mark unclear Y."
                ),
                "4. could_not_label Y leaves a case out.",
            ),
            sheets=(
                workbook.Sheet(
                    "cases",
                    CASE_COLUMNS,
                    [[c] for c in found],
                    editable=frozenset(CASE_COLUMNS[1:]),
                    choices={"unclear": ("Y", "N"), "could_not_label": ("Y", "N")},
                ),
                workbook.Sheet("claims", ("case_no", "claim_no", "statement"), claims, wide=frozenset({"statement"})),
                workbook.Sheet(
                    "requests", ("case_no", "request", "type", "for_claims"), requests, wide=frozenset({"request"})
                ),
            ),
            frozen={c: ref for c, (_, ref) in found.items()},
        )
        return missing

    def read(self, paths: Sequence[Path], ctx) -> GoldSet:
        from cfr_v2.selection.selector import claim_order, selector_inputs

        gold, rows, frozen = GoldSet(), {}, {}
        for path in paths:
            try:
                found_rows = workbook.read(path, "cases", CASE_COLUMNS)
            except workbook.WorkbookError as exc:
                gold.problems.append(str(exc))
                continue
            refs = workbook.frozen(path)
            for row_no, r in found_rows:
                if not r["case_no"] or r["could_not_label"] == "Y":
                    continue
                if r["case_no"] in rows:  # labelled twice: the first workbook given is used
                    first = rows[r["case_no"]][1]
                    if (first["primary_claim_no"], first["unclear"]) != (r["primary_claim_no"], r["unclear"]):
                        gold.problems.append(f"{r['case_no']}: labellers disagree; {path.name}'s answer is not used")
                    continue
                rows[r["case_no"]] = (row_no, r)
                frozen[r["case_no"]] = refs.get(r["case_no"], "")
        found, missing = self._claim_sets(ctx, sorted(rows))
        gold.problems += missing
        for case_no, (claim_set, ref) in found.items():
            row_no, r = rows[case_no]
            if frozen.get(case_no) and frozen[case_no] != ref:
                gold.problems.append(f"{case_no}: the claim set changed since the template; make a new template")
                continue
            ids = tuple(c.claim_id for c in claim_order(claim_set))
            unclear = r["unclear"] == "Y"
            number = workbook.numbers(r["primary_claim_no"])
            if unclear and number:
                gold.problems.append(f"{case_no}: cases row {row_no}: unclear Y with a claim number; choose one")
                continue
            if not unclear and (not number or len(number) != 1 or not 1 <= number[0] <= len(ids)):
                gold.problems.append(f"{case_no}: cases row {row_no}: primary_claim_no must be one of 1 to {len(ids)}")
                continue
            label = Label(None if unclear else ids[number[0] - 1], unclear, ids)
            inputs = selector_inputs(claim_set) | {"claim_ids": list(ids)}
            gold.examples.append(GoldExample(case_no, inputs, label))
        return gold

    def program(self, ctx) -> dspy.Module:
        from cfr_v2.selection.selector import Selector

        return Selector()

    def metric(self, label: Label, prediction: dspy.Prediction) -> Score:
        answer = prediction.selection
        status = str(getattr(answer, "status", "") or "")
        chosen = getattr(answer, "claim_id", None)
        candidates = set(getattr(answer, "candidate_claim_ids", None) or ())
        name = {c: f"claim {i}" for i, c in enumerate(label.numbers, start=1)}
        if label.unclear:
            if status == "AMBIGUOUS":
                return Score(1.0, "Right: the claims are equally central.")
            return Score(
                0.0, f"Chose {name.get(chosen, chosen)}, but the claims are equally central: answer AMBIGUOUS."
            )
        if status == "SELECTED" and chosen == label.primary:
            return Score(1.0, "Right claim.")
        if status == "AMBIGUOUS":
            partial = label.primary in candidates
            return Score(
                0.25 if partial else 0.0,
                f"Answered AMBIGUOUS, but {name[label.primary]} is clearly the main complaint.",
            )
        return Score(0.0, f"Chose {name.get(chosen, chosen)}; the main complaint is {name[label.primary]}.")


SPEC = SelectionSpec()

__all__ = ["SPEC", "Label", "SelectionSpec"]
