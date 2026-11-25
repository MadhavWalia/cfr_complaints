"""Selection (C03): is the primary claim right? (LDR-0042 §5.4; the metric is LDR-0018's)

**Workbook** (sheet ``cases``, one row per case): the claims numbered C1, C2, … in the order the
customer raised them (the order the selector sees), the requests, C03's answer, its primary or
candidates and its reasoning; for the reviewer, Correct? (Y/N) and Right primary (if not): a claim
number, or Unclear when two or more claims are equally central. Hidden Ref.

**Cases with one claim** are decided without the model (Q9): exported, marked, left out of the
headline and reported on their own.

**Truth** for a case: Correct = Y agrees with the answer (the selected claim; Unclear for
AMBIGUOUS; none for NO_ELIGIBLE_CLAIM); Correct = N takes the Right primary given.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .report import Rate, Report, Table
from .source import Reader, Results, Run
from .stage import CASE_NO, NOTES, REF, ExportContext, lines, ref, stamp
from .workbook import YN, Column, Read, Review, Row, Sheet

SHEET = "cases"
UNCLEAR = "Unclear"
NONE = "No eligible claim"
NUMBERS = tuple(f"C{i}" for i in range(1, 100))


def ordered_claims(pass2: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The claims in the order the customer raised them: by their earliest allegation, as
    ``selection.selector.claim_order`` sorts them."""
    position = {a.get("allegation_id"): i for i, a in enumerate(pass2.get("allegations") or ())}
    claims = [c for c in pass2.get("claims") or () if isinstance(c, Mapping)]
    return sorted(
        claims,
        key=lambda c: min(
            (position.get(a, len(position)) for a in c.get("allegation_ids") or ()), default=len(position)
        ),
    )


class Selection:
    key = "selection"
    title = "Selection (C03)"
    task = "c03.primary_selection"
    tasks = ("c02.pass2", "c03.primary_selection")
    headline = "Top-1 accuracy"

    def sheets(self, choices: Mapping[str, tuple[str, ...]] | None = None) -> tuple[Sheet, ...]:
        return (
            Sheet(
                SHEET,
                (
                    CASE_NO,
                    Column(
                        "Claims",
                        70,
                        wrap=True,
                        note="Every claim, numbered in the order the customer raised them: the order C03 sees",
                    ),
                    Column(
                        "Requests",
                        50,
                        wrap=True,
                        note="What the customer asked for, and the claims each request is about",
                    ),
                    Column(
                        "Answer",
                        20,
                        note="SELECTED, AMBIGUOUS (two or more claims equally central) or NO_ELIGIBLE_CLAIM",
                    ),
                    Column("Primary", 50, wrap=True, note="The claim C03 chose, when it chose one"),
                    Column("Candidates", 16, note="When AMBIGUOUS: the claims it couldn't choose between"),
                    Column("Reasoning", 60, wrap=True),
                    Column(
                        "Decided without the model",
                        14,
                        note="Y when the case has one claim: it is the primary, and the model isn't asked",
                    ),
                    Column(
                        "Correct? (Y/N)",
                        14,
                        review=True,
                        choices=YN,
                        note="For the reviewer: is the answer right? For AMBIGUOUS, Y means the claims really are equally central",
                    ),
                    Column(
                        "Right primary (if not)",
                        18,
                        review=True,
                        choices=(*NUMBERS, UNCLEAR, NONE),
                        note="For the reviewer: the claim number that is the main complaint, Unclear when two or more are equally central, or No eligible claim",
                    ),
                    NOTES,
                    REF,
                ),
                key=("Ref",),
            ),
        )

    def choices(self, ctx: ExportContext, rows_seen: Iterable[Row]) -> Mapping[str, tuple[str, ...]]:
        return {}

    def rows(self, run: Run, results: Results, reader: Reader) -> Iterable[Row]:
        pass2, selection = results.get("c02.pass2"), results.get(self.task)
        if not isinstance(pass2, Mapping) or not isinstance(selection, Mapping):
            return
        claims = ordered_claims(pass2)
        number = {c.get("claim_id"): f"C{i}" for i, c in enumerate(claims, start=1)}
        allegation_claim = {a: c.get("claim_id") for c in claims for a in c.get("allegation_ids") or ()}
        requests = []
        for r in pass2.get("requests") or ():
            about = sorted(
                {
                    number[allegation_claim[a]]
                    for a in r.get("allegation_ids") or ()
                    if allegation_claim.get(a) in number
                }
            )
            requests.append(f"{r.get('text', '')}" + (f" (about {', '.join(about)})" if about else ""))
        chosen = selection.get("claim_id")
        primary = next((c for c in claims if c.get("claim_id") == chosen), None)
        yield Row(
            SHEET,
            (
                run.case_no,
                lines(f"{number[c.get('claim_id')]} {c.get('statement', '')}" for c in claims),
                lines(requests),
                selection.get("status", ""),
                f"{number[chosen]} {primary.get('statement', '')}" if primary else "",
                ", ".join(number.get(c, c) for c in selection.get("candidate_claim_ids") or ()),
                selection.get("rationale") or "",
                "Y" if len(claims) < 2 else "N",
                ref(run),
            ),
            stamp(run, results, self.task),
        )

    def score(self, review: Review) -> Report:
        rows = review.rows[SHEET]
        reviewed = [Case(r) for r in rows if r["Correct? (Y/N)"] is not None]
        model = [c for c in reviewed if not c.without_model]
        single = [c for c in reviewed if c.without_model]
        clear = [c for c in model if c.truth not in (None, UNCLEAR, NONE)]

        report = Report(self.key, self.headline, Rate(sum(c.top1 for c in clear), len(clear)))
        report.counts = {
            "cases": len(rows),
            "reviewed": len(reviewed),
            "not reviewed": len(rows) - len(reviewed),
            "decided without the model": len(single),
            "reviewed but no right primary given": sum(1 for c in reviewed if c.truth is None and c.answer_wrong),
        }

        main = Table("Primary claim", ("Measure", "Value"))
        main.add("Top-1 accuracy (clear cases)", Rate(sum(c.top1 for c in clear), len(clear)))
        scored = [c for c in model if c.truth is not None]
        main.add("Weighted score", Rate(sum(c.weighted for c in scored), len(scored)))
        ambiguous = [c for c in model if c.answer == "AMBIGUOUS"]
        main.add(
            "AMBIGUOUS answers the reviewer agrees with",
            Rate(sum(c.truth == UNCLEAR for c in ambiguous), len(ambiguous)),
        )
        main.add("AMBIGUOUS rate", Rate(len(ambiguous), len(model)))
        none = [c for c in model if c.answer == "NO_ELIGIBLE_CLAIM"]
        main.add(
            "NO_ELIGIBLE_CLAIM answers the reviewer agrees with", Rate(sum(c.truth == NONE for c in none), len(none))
        )
        main.add("Cases decided without the model, right", Rate(sum(c.correct for c in single), len(single)))
        main.note = "Weighted: 1 when the primary is right, 0.25 when AMBIGUOUS lists the right claim, 1 for AMBIGUOUS when the reviewer says Unclear (as the C03 gold metric). Cases with one claim are only in the last line."
        report.tables.append(main)

        by_count = Table("By number of claims", ("Claims", "Clear cases", "Top-1 accuracy"))
        for label, keep in (("2", lambda n: n == 2), ("3", lambda n: n == 3), ("4 or more", lambda n: n >= 4)):
            group = [c for c in clear if keep(c.claims)]
            by_count.add(label, len(group), Rate(sum(c.top1 for c in group), len(group)))
        report.tables.append(by_count)

        position = Table("Where the main complaint was raised", ("Right primary", "Cases", "Top-1 accuracy"))
        for label, keep in (
            ("First claim raised (C1)", lambda t: t == "C1"),
            ("Second (C2)", lambda t: t == "C2"),
            ("Later", lambda t: t not in ("C1", "C2")),
        ):
            group = [c for c in clear if keep(c.truth)]
            position.add(label, len(group), Rate(sum(c.top1 for c in group), len(group)))
        report.tables.append(position)
        return report


class Case:
    def __init__(self, row: Read):
        self.answer = row["Answer"] or ""
        self.without_model = row["Decided without the model"] == "Y"
        self.claims = len((row["Claims"] or "").splitlines())
        chosen = (row["Primary"] or "").split(" ", 1)[0] or None
        candidates = [c.strip() for c in (row["Candidates"] or "").split(",") if c.strip()]
        self.correct = row["Correct? (Y/N)"] == "Y"
        self.answer_wrong = row["Correct? (Y/N)"] == "N"
        if self.correct:
            self.truth = {"SELECTED": chosen, "AMBIGUOUS": UNCLEAR, "NO_ELIGIBLE_CLAIM": NONE}.get(self.answer)
        else:
            self.truth = row["Right primary (if not)"]
        self.top1 = 1.0 if self.answer == "SELECTED" and chosen == self.truth else 0.0
        if self.truth == UNCLEAR:
            self.weighted = 1.0 if self.answer == "AMBIGUOUS" else 0.0
        elif self.answer == "AMBIGUOUS" and self.truth in candidates:
            self.weighted = 0.25
        else:
            self.weighted = self.top1 if self.truth != NONE else float(self.answer == "NO_ELIGIBLE_CLAIM")
