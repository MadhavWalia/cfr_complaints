"""Gold spec for C02 Pass 2 (LDR-0027): claims from Pass 1's items.

**Template:** per case, its committed Pass 1 items (allegations numbered as the stage numbers
them, requests, agent statements) on locked sheets; the labeller writes the claims on ``claims``:
which allegations each one groups, whether it is about an account, its event window and how well
the dates are known, and up to three evidence source types (dropdowns from the current release's
taxonomy). A case with no claims is marked on ``cases``.

**Inputs:** exactly what the stage sends: Pass 1's texts and links, the customer's accounts from
the baseline, the received date (``pass2.program_inputs``), frozen from the committed Pass 1
result named in the template.

**Metric:** claims match when the allegations they group overlap (Jaccard ≥ 0.5, greedily); the
score is half claim F1, half agreement on the matched claims' account flag, time quality, window
and evidence sources. Feedback names counts, the fields that disagreed and source type ids.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import dspy

from . import workbook
from .gold import GoldExample, GoldSet, Score

TIME_QUALITY = ("EXACT", "APPROXIMATE", "UNKNOWN")
MATCH = 0.5
CLAIM_COLUMNS = (
    "case_no",
    "claim_no",
    "allegation_nos",
    "about_an_account",
    "event_window_start",
    "event_window_end",
    "time_quality",
    "evidence_1",
    "evidence_2",
    "evidence_3",
    "notes",
)
CASE_COLUMNS = ("case_no", "labeller", "no_claims", "could_not_label", "notes")


@dataclass(frozen=True)
class GoldClaim:
    allegations: frozenset[str]
    about_an_account: bool
    window: tuple[str | None, str | None]
    time_quality: str
    sources: frozenset[str]


class Pass2Spec:
    prompt_id = "c02.pass2"

    def _pass1(self, ctx, case_nos: Sequence[str]):
        """case_no -> (run, Pass1Result, its ref) for cases with a committed Pass 1; and the rest, with why."""
        from cfr_v2.contracts import Pass1Result

        runs, found, missing = ctx.runs(case_nos), {}, []
        for case_no in case_nos:
            if case_no not in runs:
                missing.append(f"{case_no}: no case run in the state database")
                continue
            result, ref = ctx.committed(runs[case_no][0], "c02.pass1", Pass1Result)
            if result is None:
                missing.append(f"{case_no}: no committed Pass 1 result; run the worker or a trial first")
            else:
                found[case_no] = (runs[case_no], result, ref)
        return found, missing

    def template(self, case_ids: Sequence[str], out: Path, ctx) -> list[str]:
        found, missing = self._pass1(ctx, case_ids)
        allegations, requests, statements = [], [], []
        for case_no, (_, result, _) in found.items():
            number = {a.allegation_id: i for i, a in enumerate(result.allegations, start=1)}
            r_number = {r.request_id: i for i, r in enumerate(result.requests, start=1)}
            for i, a in enumerate(result.allegations, start=1):
                allegations.append([case_no, i, a.text, a.kind.value, a.scope.value])
            for i, r in enumerate(result.requests, start=1):
                requests.append([case_no, i, r.text, r.request_type.value, _nos(r.allegation_ids, number)])
            for i, s in enumerate(result.agent_statements, start=1):
                statements.append(
                    [case_no, i, s.text, s.kind.value, _nos(s.allegation_ids, number), _nos(s.request_ids, r_number)]
                )
        sources = [t.id for t in ctx.taxonomy.source_types]
        workbook.write(
            out,
            title=f"Gold set for C02 Pass 2: claims ({len(found)} cases)",
            steps=(
                (
                    "1. For each case, read its allegations, requests and agent statements (grey sheets: Pass 1's output, "
                    "locked; allegation numbers are the ones to use)."
                ),
                (
                    "2. On 'claims', write one row per claim: the case, a claim number, and the numbers of the allegations "
                    "it groups (e.g. 1, 3). Every allegation belongs to exactly one claim."
                ),
                (
                    "3. about_an_account: Y unless the claim concerns no account. Event window: YYYY-MM-DD, or empty when "
                    "unknown. time_quality: how the dates are known."
                ),
                (
                    "4. evidence_1 to evidence_3: where the evidence that would confirm or refute the claim would be found "
                    f"(the {ctx.taxonomy.version} source types)."
                ),
                (
                    "5. On 'cases', mark no_claims Y for a case with nothing to claim, and could_not_label Y to leave a case "
                    "out."
                ),
            ),
            sheets=(
                workbook.Sheet(
                    "cases",
                    CASE_COLUMNS,
                    [[c] for c in found],
                    editable=frozenset(CASE_COLUMNS[1:]),
                    choices={"no_claims": ("Y", "N"), "could_not_label": ("Y", "N")},
                ),
                workbook.Sheet(
                    "claims",
                    CLAIM_COLUMNS,
                    free_rows=True,
                    wide=frozenset({"notes"}),
                    choices={
                        "case_no": list(found),
                        "about_an_account": ("Y", "N"),
                        "time_quality": TIME_QUALITY,
                        "evidence_1": sources,
                        "evidence_2": sources,
                        "evidence_3": sources,
                    },
                ),
                workbook.Sheet(
                    "allegations",
                    ("case_no", "allegation_no", "allegation", "kind", "about"),
                    allegations,
                    wide=frozenset({"allegation"}),
                ),
                workbook.Sheet(
                    "requests",
                    ("case_no", "request_no", "request", "type", "for_allegations"),
                    requests,
                    wide=frozenset({"request"}),
                ),
                workbook.Sheet(
                    "agent_statements",
                    ("case_no", "statement_no", "statement", "kind", "responds_to_allegations", "responds_to_requests"),
                    statements,
                    wide=frozenset({"statement"}),
                ),
            ),
            frozen={c: ref for c, (_, _, ref) in found.items()},
        )
        return missing

    def read(self, paths: Sequence[Path], ctx) -> GoldSet:
        from cfr_v2.contracts import BaselineProfile
        from cfr_v2.understanding.handler import received_date
        from cfr_v2.understanding.pass2 import program_inputs

        gold = GoldSet()
        cases: dict[str, dict] = {}
        claims: dict[str, list[tuple[int, dict]]] = {}
        frozen: dict[str, str] = {}
        sources = {t.id for t in ctx.taxonomy.source_types}
        for path in paths:
            try:
                rows = workbook.read(path, "cases", CASE_COLUMNS)
                claim_rows = workbook.read(path, "claims", CLAIM_COLUMNS)
            except workbook.WorkbookError as exc:
                gold.problems.append(str(exc))
                continue
            refs = workbook.frozen(path)
            for _, r in rows:
                if r["case_no"] and r["case_no"] not in cases and r["could_not_label"] != "Y":
                    cases[r["case_no"]] = r
                    frozen[r["case_no"]] = refs.get(r["case_no"], "")
                    claims[r["case_no"]] = [(n, c) for n, c in claim_rows if c["case_no"] == r["case_no"]]
        found, missing = self._pass1(ctx, sorted(cases))
        gold.problems += missing
        for case_no, (run, result, ref) in found.items():
            if frozen.get(case_no) and frozen[case_no] != ref:
                gold.problems.append(f"{case_no}: Pass 1 has a newer result than the template; make a new template")
                continue
            labelled = claims.get(case_no, [])
            if not labelled and cases[case_no].get("no_claims") != "Y":
                gold.problems.append(f"{case_no}: no claims and no_claims isn't Y; left out")
                continue
            ids = [a.allegation_id for a in result.allegations]
            label, problems = _claims(case_no, labelled, ids, sources)
            gold.problems += problems
            if problems:
                continue
            request = ctx.request(run[1])
            baseline = (
                BaselineProfile.model_validate_json(ctx.get(request.baseline_ref)) if request.baseline_ref else None
            )
            inputs = program_inputs(result, baseline, received_date(ctx.store, request, baseline))
            gold.examples.append(GoldExample(case_no, inputs, tuple(label)))
        return gold

    def program(self, ctx) -> dspy.Module:
        from cfr_v2.understanding.pass2 import Pass2

        return Pass2(ctx.taxonomy)

    def metric(self, label: tuple[GoldClaim, ...], prediction: dspy.Prediction) -> Score:
        predicted = [_from_prediction(c) for c in (prediction.claims or [])]
        pairs = _match(list(label), predicted)
        g, p, m = len(label), len(predicted), len(pairs)
        claim_f1 = 1.0 if g == p == 0 else (2 * m / (g + p))
        fields, notes = [], []
        if g != m or p != m:
            notes.append(f"claims: {m} of {g} matched by their allegations; {p - m} predicted claims match none")
        wrong = {"account": 0, "time quality": 0, "window": 0}
        missed_sources, extra_sources = set(), set()
        for gi, pi in pairs:
            a, b = label[gi], predicted[pi]
            wrong["account"] += a.about_an_account != b.about_an_account
            wrong["time quality"] += a.time_quality != b.time_quality
            wrong["window"] += a.window != b.window
            missed_sources |= a.sources - b.sources
            extra_sources |= b.sources - a.sources
            fields.append(
                (
                    (a.about_an_account == b.about_an_account)
                    + (a.time_quality == b.time_quality)
                    + (a.window == b.window)
                    + _set_f1(a.sources, b.sources)
                )
                / 4
            )
        notes += [f"{name} wrong on {n} of {m} matched claims" for name, n in wrong.items() if n]
        if missed_sources:
            notes.append(f"evidence sources missed: {', '.join(sorted(missed_sources))}")
        if extra_sources:
            notes.append(f"evidence sources not needed: {', '.join(sorted(extra_sources))}")
        field_score = sum(fields) / len(fields) if fields else (1.0 if g == p == 0 else 0.0)
        return Score(0.5 * claim_f1 + 0.5 * field_score, "; ".join(notes) or "Claims and their fields all right.")


def _nos(ids: Sequence[str], number: dict[str, int]) -> str:
    return ", ".join(str(number[i]) for i in ids if i in number)


def _claims(case_no: str, rows, ids: list[str], sources: set[str]) -> tuple[list[GoldClaim], list[str]]:
    out, problems, used = [], [], set()
    for row_no, r in rows:
        where = f"{case_no}: claims row {row_no}"
        nos = workbook.numbers(r["allegation_nos"])
        if not nos:
            problems.append(f"{where}: allegation_nos needs allegation numbers, e.g. 1, 3")
            continue
        if any(n < 1 or n > len(ids) for n in nos):
            problems.append(f"{where}: an allegation number isn't one of the case's 1 to {len(ids)}")
            continue
        if used & set(nos):
            problems.append(f"{where}: an allegation is already in another claim")
        used |= set(nos)
        window = (_date(r["event_window_start"]), _date(r["event_window_end"]))
        if (r["event_window_start"] and not window[0]) or (r["event_window_end"] and not window[1]):
            problems.append(f"{where}: event window dates must be YYYY-MM-DD")
        quality = (r["time_quality"] or "").upper()
        if quality not in TIME_QUALITY:
            problems.append(f"{where}: time_quality must be one of {', '.join(TIME_QUALITY)}")
        chosen = {r[f"evidence_{i}"] for i in (1, 2, 3) if r[f"evidence_{i}"]}
        if chosen - sources:
            problems.append(f"{where}: unknown evidence source {', '.join(sorted(chosen - sources))}")
        if r["about_an_account"] not in ("Y", "N"):
            problems.append(f"{where}: about_an_account must be Y or N")
        out.append(
            GoldClaim(
                frozenset(ids[n - 1] for n in nos), r["about_an_account"] == "Y", window, quality, frozenset(chosen)
            )
        )
    return out, problems


def _date(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _from_prediction(claim) -> GoldClaim:
    needs = getattr(claim, "evidence_needs", None) or []
    return GoldClaim(
        frozenset(getattr(claim, "allegation_ids", None) or ()),
        bool(getattr(claim, "about_an_account", True)),
        (_date(getattr(claim, "event_window_start", None)), _date(getattr(claim, "event_window_end", None))),
        str(getattr(claim, "time_quality", "") or ""),
        frozenset(str(getattr(n, "source_type", "")) for n in needs if getattr(n, "source_type", None)),
    )


def _match(gold: list[GoldClaim], predicted: list[GoldClaim]) -> list[tuple[int, int]]:
    """Gold index -> predicted index, one-to-one, by the overlap of the allegations they group."""
    pairs = sorted(
        ((_jaccard(g.allegations, p.allegations), i, j) for i, g in enumerate(gold) for j, p in enumerate(predicted)),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    used_g, used_p, out = set(), set(), []
    for score, i, j in pairs:
        if score < MATCH:
            break
        if i not in used_g and j not in used_p:
            used_g.add(i)
            used_p.add(j)
            out.append((i, j))
    return out


def _jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _set_f1(gold: frozenset, predicted: frozenset) -> float:
    if not gold and not predicted:
        return 1.0
    shared = len(gold & predicted)
    return 2 * shared / (len(gold) + len(predicted))


SPEC = Pass2Spec()

__all__ = ["SPEC", "GoldClaim", "Pass2Spec"]
