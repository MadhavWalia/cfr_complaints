"""Scoring Pass 1 against the gold set (Phase C exit; lld/guides/gold-set-allegations.md, section 13).

Both sides are turned into the same neutral form: items with a kind, transcript spans and
links. Items on the same list match when their spans overlap enough (the overlap divided
by the shorter of the two, at least ``min_overlap``), greedily from the largest overlap.
The same code compares the pipeline with a labeller and one labeller with another.

Gold quotes are located in the redacted transcript; a quote with redacted text in it
(a name, say) is located in the original and mapped across with the redaction map.
Reports carry counts, case numbers and sheet rows only, never text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from cfr_v2.contracts import ClaimSetRevision, Pass1Result, PrimarySelection, RedactionMap, SelectionStatus, SpanRef

from .gold import GoldCase
from .spans import locate

LISTS = ("allegations", "details", "requests", "statements")


@dataclass(frozen=True)
class CaseText:
    """One case's transcript: redacted texts (what spans index), originals with their redaction maps."""

    segments: tuple[tuple[str, str], ...]
    originals: tuple[tuple[str, str, RedactionMap], ...] = ()

    def locate(self, quote: str) -> SpanRef | None:
        span = locate(quote, self.segments)
        if span is not None:
            return span
        for interaction_id, original, redaction_map in self.originals:
            found = locate(quote, [(interaction_id, original)])
            if found is not None:
                start, end = redaction_map.to_redacted(found.start, found.end)
                return SpanRef(interaction_id=interaction_id, start=start, end=end)
        return None


@dataclass(frozen=True)
class Item:
    key: str
    kind: str
    spans: tuple[SpanRef, ...]
    about: str | None = None
    allegations: frozenset[str] = frozenset()
    requests: frozenset[str] = frozenset()
    ambiguous: bool = False


@dataclass
class Side:
    allegations: list[Item] = field(default_factory=list)
    requests: list[Item] = field(default_factory=list)
    statements: list[Item] = field(default_factory=list)
    details: dict[str, list[Item]] = field(default_factory=dict)  # by allegation key


@dataclass(frozen=True)
class QuoteProblem:
    """A gold quote not found in the transcript: sent back to the labeller (source, sheet, row)."""

    case_no: str
    source: str
    sheet: str
    row: int


def gold_side(case: GoldCase, text: CaseText) -> tuple[Side, list[QuoteProblem]]:
    problems: list[QuoteProblem] = []

    def spans(quotes: Sequence[str], sheet: str, row: int) -> tuple[SpanRef, ...]:
        found = tuple(s for s in (text.locate(q) for q in quotes) if s is not None)
        if len(found) < len(quotes):
            problems.append(QuoteProblem(case.case_no, case.source, sheet, row))
        return found

    side = Side()
    for a in case.allegations:
        side.allegations.append(
            Item(f"a{a.number}", a.kind, spans(a.quotes, "allegations", a.row), a.about, ambiguous=a.ambiguous)
        )
    for d in case.details:
        side.details.setdefault(f"a{d.allegation}", []).append(
            Item(f"d{d.row}", d.kind, spans((d.quote,), "details", d.row))
        )
    for q in case.requests:
        side.requests.append(
            Item(
                f"r{q.number}",
                q.kind,
                spans(q.quotes, "requests", q.row),
                allegations=frozenset(f"a{n}" for n in q.allegations),
                ambiguous=q.ambiguous,
            )
        )
    for s in case.statements:
        side.statements.append(
            Item(
                f"s{s.number}",
                s.kind,
                spans(s.quotes, "agent_statements", s.row),
                allegations=frozenset(f"a{n}" for n in s.allegations),
                requests=frozenset(f"r{n}" for n in s.requests),
                ambiguous=s.ambiguous,
            )
        )
    return side, problems


def pipeline_side(result: Pass1Result) -> Side:
    side = Side()
    for a in result.allegations:
        side.allegations.append(Item(a.allegation_id, a.kind.value, a.spans, a.scope.value, ambiguous=a.ambiguous))
        side.details[a.allegation_id] = [
            Item(f"{a.allegation_id}.d{i}", d.detail_type.value, (d.span,)) for i, d in enumerate(a.details)
        ]
    for r in result.requests:
        side.requests.append(Item(r.request_id, r.request_type.value, r.spans, allegations=frozenset(r.allegation_ids)))
    for s in result.agent_statements:
        side.statements.append(
            Item(
                s.statement_id,
                s.kind.value,
                s.spans,
                allegations=frozenset(s.allegation_ids),
                requests=frozenset(s.request_ids),
            )
        )
    return side


def overlap(a: Sequence[SpanRef], b: Sequence[SpanRef]) -> float:
    """Shared characters over the shorter side's characters; 0 when either side has none."""
    shared = sum(
        max(0, min(x.end, y.end) - max(x.start, y.start)) for x in a for y in b if x.interaction_id == y.interaction_id
    )
    shorter = min(sum(x.end - x.start for x in a), sum(y.end - y.start for y in b))
    return shared / shorter if shorter else 0.0


def match(gold: Sequence[Item], predicted: Sequence[Item], min_overlap: float) -> dict[str, str]:
    """Gold key -> predicted key, one-to-one, largest overlaps first."""
    pairs = sorted(
        ((overlap(g.spans, p.spans), i, j) for i, g in enumerate(gold) for j, p in enumerate(predicted)),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    used_g: set[int] = set()
    used_p: set[int] = set()
    matched: dict[str, str] = {}
    for score, i, j in pairs:
        if score < min_overlap:
            break
        if i in used_g or j in used_p:
            continue
        used_g.add(i)
        used_p.add(j)
        matched[gold[i].key] = predicted[j].key
    return matched


@dataclass
class ListScore:
    gold: int = 0
    predicted: int = 0
    matched: int = 0
    same_kind: int = 0
    same_about: int = 0  # allegations only
    links_checked: int = 0
    links_correct: int = 0
    gold_unambiguous: int = 0
    matched_unambiguous: int = 0

    def add(self, other: ListScore) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    @property
    def precision(self) -> float | None:
        return self.matched / self.predicted if self.predicted else None

    @property
    def recall(self) -> float | None:
        return self.matched / self.gold if self.gold else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)

    @property
    def kind_agreement(self) -> float | None:
        return self.same_kind / self.matched if self.matched else None

    @property
    def about_agreement(self) -> float | None:
        return self.same_about / self.matched if self.matched else None

    @property
    def link_accuracy(self) -> float | None:
        return self.links_correct / self.links_checked if self.links_checked else None

    @property
    def unambiguous_recall(self) -> float | None:
        return self.matched_unambiguous / self.gold_unambiguous if self.gold_unambiguous else None

    def as_dict(self) -> dict[str, float | int | None]:
        values = {name: getattr(self, name) for name in self.__dataclass_fields__}
        for name in (
            "precision",
            "recall",
            "f1",
            "kind_agreement",
            "about_agreement",
            "link_accuracy",
            "unambiguous_recall",
        ):
            value = getattr(self, name)
            values[name] = round(value, 4) if value is not None else None
        return values


def _score_list(
    gold: Sequence[Item],
    predicted: Sequence[Item],
    min_overlap: float,
    *,
    links: dict[str, dict[str, str]] | None = None,
) -> tuple[ListScore, dict[str, str]]:
    usable = [g for g in gold if g.spans]  # a gold item whose quotes can't be found can't be scored
    matched = match(usable, predicted, min_overlap)
    by_key = {p.key: p for p in predicted}
    score = ListScore(gold=len(usable), predicted=len(predicted), matched=len(matched))
    for g in usable:
        score.gold_unambiguous += not g.ambiguous
        p = by_key.get(matched.get(g.key, ""))
        if p is None:
            continue
        score.matched_unambiguous += not g.ambiguous
        score.same_kind += g.kind == p.kind
        score.same_about += g.about is not None and g.about == p.about
        if links is not None:
            # A link is right when the predicted item links to the counterparts of the gold item's links.
            to_gold = {name: {pk: gk for gk, pk in m.items()} for name, m in links.items()}
            same = {pk_name: {to_gold[pk_name].get(k, f"?{k}") for k in getattr(p, pk_name)} for pk_name in links}
            score.links_checked += 1
            score.links_correct += all(same[name] == set(getattr(g, name)) for name in links)
    return score, matched


def score_sides(gold: Side, predicted: Side, min_overlap: float) -> dict[str, ListScore]:
    allegations, a_match = _score_list(gold.allegations, predicted.allegations, min_overlap)
    details = ListScore()
    for g_key, p_key in a_match.items():
        d, _ = _score_list(gold.details.get(g_key, []), predicted.details.get(p_key, []), min_overlap)
        details.add(d)
    # Details of unmatched gold allegations are missed details; of unmatched predicted ones, extra.
    for g_key, items in gold.details.items():
        if g_key not in a_match:
            details.gold += sum(1 for i in items if i.spans)
            details.gold_unambiguous += sum(1 for i in items if i.spans)
    matched_p = set(a_match.values())
    details.predicted += sum(len(v) for k, v in predicted.details.items() if k not in matched_p)

    requests, r_match = _score_list(gold.requests, predicted.requests, min_overlap, links={"allegations": a_match})
    statements, _ = _score_list(
        gold.statements,
        predicted.statements,
        min_overlap,
        links={"allegations": a_match, "requests": r_match},
    )
    return {"allegations": allegations, "details": details, "requests": requests, "statements": statements}


@dataclass
class SelectionScore:
    """C03 against the gold ``primary_allegation_no`` (LDR-0018): top-1 and use of AMBIGUOUS."""

    clear: int = 0  # gold primary_unclear = N, and its allegation matched a pipeline allegation
    clear_correct: int = 0  # ...and C03 SELECTED the claim holding that allegation
    clear_ambiguous: int = 0  # ...but C03 said AMBIGUOUS
    unclear: int = 0  # gold primary_unclear = Y (matched)
    unclear_ambiguous: int = 0  # ...and C03 said AMBIGUOUS
    unmatched: int = 0  # the gold primary allegation matched no pipeline allegation

    def add(self, other: SelectionScore) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    @property
    def top1(self) -> float | None:
        return self.clear_correct / self.clear if self.clear else None

    @property
    def ambiguous_when_clear(self) -> float | None:
        return self.clear_ambiguous / self.clear if self.clear else None

    @property
    def ambiguous_when_unclear(self) -> float | None:
        return self.unclear_ambiguous / self.unclear if self.unclear else None

    def as_dict(self) -> dict[str, float | int | None]:
        values = {name: getattr(self, name) for name in self.__dataclass_fields__}
        for name in ("top1", "ambiguous_when_clear", "ambiguous_when_unclear"):
            value = getattr(self, name)
            values[name] = round(value, 4) if value is not None else None
        return values


def score_selection(
    case: GoldCase,
    gold: Side,
    predicted: Side,
    claim_set: ClaimSetRevision,
    selection: PrimarySelection,
    min_overlap: float,
) -> SelectionScore:
    """The gold main complaint is matched to a pipeline allegation, then to the claim that holds it."""
    score = SelectionScore()
    if case.primary is None:
        return score
    allegation = match([g for g in gold.allegations if g.spans], predicted.allegations, min_overlap).get(
        f"a{case.primary}"
    )
    claim = next((c.claim_id for c in claim_set.claims if allegation in c.allegation_ids), None)
    if claim is None:
        score.unmatched = 1
        return score
    ambiguous = selection.status is SelectionStatus.AMBIGUOUS
    if case.primary_unclear:
        score.unclear, score.unclear_ambiguous = 1, int(ambiguous)
    else:
        score.clear, score.clear_ambiguous = 1, int(ambiguous)
        score.clear_correct = int(selection.status is SelectionStatus.SELECTED and selection.claim_id == claim)
    return score


@dataclass
class Report:
    """Totals over cases; per-category totals for allegations; inter-labeller agreement."""

    totals: dict[str, ListScore] = field(default_factory=lambda: {n: ListScore() for n in LISTS})
    allegations_by_category: dict[str, ListScore] = field(default_factory=dict)
    agreement: dict[str, ListScore] = field(default_factory=lambda: {n: ListScore() for n in LISTS})
    cases_scored: list[str] = field(default_factory=list)
    cases_without_prediction: list[str] = field(default_factory=list)
    cases_not_labelled: list[str] = field(default_factory=list)
    agreement_cases: list[str] = field(default_factory=list)
    quote_problems: list[QuoteProblem] = field(default_factory=list)
    selection: SelectionScore = field(default_factory=SelectionScore)
    cases_without_selection: list[str] = field(default_factory=list)
    pipeline_kept: int = 0
    pipeline_dropped: int = 0
    program_versions: dict[str, int] = field(default_factory=dict)

    def add_case(self, case_no: str, scores: dict[str, ListScore], category: str | None, result: Pass1Result) -> None:
        self.cases_scored.append(case_no)
        for name, score in scores.items():
            self.totals[name].add(score)
        self.allegations_by_category.setdefault(category or "(none)", ListScore()).add(scores["allegations"])
        self.pipeline_kept += (
            len(result.allegations)
            + sum(len(a.details) for a in result.allegations)
            + len(result.requests)
            + len(result.agent_statements)
        )
        self.pipeline_dropped += len(result.dropped)
        self.program_versions[result.program_version] = self.program_versions.get(result.program_version, 0) + 1

    @property
    def dropped_rate(self) -> float | None:
        total = self.pipeline_kept + self.pipeline_dropped
        return self.pipeline_dropped / total if total else None

    def as_dict(self) -> dict:
        return {
            "cases_scored": len(self.cases_scored),
            "cases_without_prediction": self.cases_without_prediction,
            "cases_not_labelled": self.cases_not_labelled,
            "program_versions": self.program_versions,
            "pipeline_dropped_rate": round(self.dropped_rate, 4) if self.dropped_rate is not None else None,
            "totals": {n: s.as_dict() for n, s in self.totals.items()},
            "allegations_by_category": {c: s.as_dict() for c, s in sorted(self.allegations_by_category.items())},
            "agreement_cases": self.agreement_cases,
            "agreement": {n: s.as_dict() for n, s in self.agreement.items()},
            "selection": self.selection.as_dict(),
            "cases_without_selection": self.cases_without_selection,
            "gold_quote_problems": [p.__dict__ for p in self.quote_problems],
        }
