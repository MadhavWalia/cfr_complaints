"""Pass 1 (C02): are the allegations, requests and agent statements right? (LDR-0042 §5.2)

**Workbook.** Sheet ``items``, one row per item of the three lists: its text, type, scope (an
allegation's "about"), details, links, ambiguity, and **every quote it was built from**, from the
redacted transcript (Q3); for the reviewer, Correct?, Right type (if not), Quotes support it? and
Should not be here because. Sheet ``cases``, one row per case: the counts and what was dropped; for
the reviewer, how many of each list were missed, and what.

**Recall** is estimated from the missed counts, per list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .report import Rate, Report, Table, mix, share, spread
from .source import Reader, Results, Run
from .stage import CASE_NO, NOTES, REF, ExportContext, lines, ref, stamp
from .workbook import YPN, Column, Review, Row, Sheet

ITEMS, CASES = "items", "cases"
LISTS = (
    ("allegations", "Allegation", "allegation_id", "kind"),
    ("requests", "Request", "request_id", "request_type"),
    ("agent_statements", "Agent statement", "statement_id", "kind"),
)
TYPES = {
    "Allegation": ("CLAIM", "IMPACT"),
    "Request": (
        "refund_or_credit",
        "fee_reversal",
        "cancel_or_close",
        "replace_card",
        "explanation",
        "escalation",
        "callback",
        "other",
    ),
    "Agent statement": ("FACT", "ACTION", "COMMITMENT", "ADVICE"),
}
REASONS = ("Not said", "Not a complaint", "Duplicate", "Agent's words, not the customer's", "Other")
MISSED = {
    "Allegation": "Missed allegations",
    "Request": "Missed requests",
    "Agent statement": "Missed agent statements",
}


class Pass1:
    key = "pass1"
    title = "Pass 1 (C02)"
    task = "c02.pass1"
    tasks = ("c02.pass1",)
    headline = "Allegation precision"

    def sheets(self, choices: Mapping[str, tuple[str, ...]] | None = None) -> tuple[Sheet, ...]:
        every_type = tuple(dict.fromkeys(t for types in TYPES.values() for t in types))
        return (
            Sheet(
                ITEMS,
                (
                    CASE_NO,
                    Column(
                        "List",
                        16,
                        note="Allegation (what the customer says went wrong, or the harm), Request (what they asked for) or Agent statement",
                    ),
                    Column("No", 6, note="The item's number in its case: a1, r1, s1"),
                    Column("Item", 60, wrap=True),
                    Column(
                        "Type",
                        18,
                        note="Allegation: CLAIM or IMPACT. Request: its type. Agent statement: FACT, ACTION, COMMITMENT or ADVICE",
                    ),
                    Column(
                        "About",
                        14,
                        note="Allegations: who or what it is about (agent, customer, system, third party, mixed)",
                    ),
                    Column(
                        "Details",
                        40,
                        wrap=True,
                        note="Allegations: the specific facts the customer gave (when, amount, merchant), one per line",
                    ),
                    Column(
                        "Linked to",
                        12,
                        note="Requests and agent statements: the allegations (and requests) they answer",
                    ),
                    Column("Ambiguous", 30, wrap=True, note="Y and why, when Pass 1 marked the item ambiguous"),
                    Column(
                        "Quotes",
                        70,
                        wrap=True,
                        note="Every passage of the redacted transcript the item was built from, one per line, in order",
                    ),
                    Column(
                        "Correct? (Y/Partly/N)",
                        16,
                        review=True,
                        choices=YPN,
                        note="For the reviewer: is it said in the call, and stated faithfully?",
                    ),
                    Column(
                        "Right type (if not)",
                        20,
                        review=True,
                        choices=every_type,
                        note="For the reviewer: the right type, from its list's types",
                    ),
                    Column("Quotes support it? (Y/Partly/N)", 18, review=True, choices=YPN),
                    Column(
                        "Should not be here because",
                        28,
                        review=True,
                        choices=REASONS,
                        note="For the reviewer, when Correct? is N",
                    ),
                    NOTES,
                    REF,
                ),
                key=("Ref",),
            ),
            Sheet(
                CASES,
                (
                    CASE_NO,
                    Column("Allegations", 12),
                    Column("Requests", 10),
                    Column("Agent statements", 12),
                    Column(
                        "Dropped",
                        10,
                        note="Items the code dropped: no quote, a quote not in the transcript, or invalid",
                    ),
                    Column(
                        "Missed allegations",
                        12,
                        review=True,
                        number=True,
                        note="For the reviewer: how many weren't found (0 when none)",
                    ),
                    Column("Missed requests", 12, review=True, number=True),
                    Column("Missed agent statements", 12, review=True, number=True),
                    Column("What was missed", 50, wrap=True, review=True),
                    NOTES,
                    REF,
                ),
                key=("Ref",),
            ),
        )

    def choices(self, ctx: ExportContext, rows_seen: Iterable[Row]) -> Mapping[str, tuple[str, ...]]:
        return {}

    def rows(self, run: Run, results: Results, reader: Reader) -> Iterable[Row]:
        pass1 = results.get(self.task)
        if not isinstance(pass1, Mapping):
            return
        stamped = stamp(run, results, self.task)
        transcript = reader.transcript(results)

        def quotes(spans: Iterable[Mapping[str, Any]]) -> str:
            if transcript is None:
                return ""
            ordered = sorted(spans, key=lambda s: (s.get("interaction_id", ""), s.get("start", 0)))
            return lines(" ".join(transcript.quote(s).split()) for s in ordered)

        counts = []
        for field_name, label, id_name, type_name in LISTS:
            items = [i for i in pass1.get(field_name) or () if isinstance(i, Mapping)]
            counts.append(len(items))
            for item in items:
                details = item.get("details") or ()
                spans = [*(item.get("spans") or ()), *(s for d in details for s in [d.get("span")] if s)]
                linked = [*(item.get("allegation_ids") or ()), *(item.get("request_ids") or ())]
                yield Row(
                    ITEMS,
                    (
                        run.case_no,
                        label,
                        item.get(id_name, ""),
                        item.get("text", ""),
                        item.get(type_name, ""),
                        item.get("scope", "") if label == "Allegation" else "",
                        lines(f"{d.get('detail_type', '')}: {d.get('text', '')}" for d in details),
                        ", ".join(linked),
                        ("Y" + (f": {item['ambiguity_note']}" if item.get("ambiguity_note") else ""))
                        if item.get("ambiguous")
                        else "",
                        quotes(spans),
                        ref(run, item.get(id_name, "")),
                    ),
                    stamped,
                )
        yield Row(CASES, (run.case_no, *counts, len(pass1.get("dropped") or ()), ref(run)), stamped)

    def score(self, review: Review) -> Report:
        items, cases = review.rows[ITEMS], review.rows[CASES]
        reviewed = [r for r in items if r["Correct? (Y/Partly/N)"] is not None]
        by_list = {label: [r for r in reviewed if r["List"] == label] for label in TYPES}
        report = Report(self.key, self.headline, share(r["Correct? (Y/Partly/N)"] for r in by_list["Allegation"]))
        report.counts = {
            "items": len(items),
            "items reviewed": len(reviewed),
            "items not reviewed": len(items) - len(reviewed),
            "cases": len(cases),
        }

        lists = Table(
            "Per list", ("List", "Reviewed", "Precision", "Strict precision", "Estimated recall", "Estimated F1")
        )
        by_case: dict[str, list[Any]] = {}
        for r in reviewed:
            by_case.setdefault((r["Ref"] or "").split("/")[0], []).append(r)
        for label, group in (*by_list.items(), ("All items", reviewed)):
            precision = share(r["Correct? (Y/Partly/N)"] for r in group)
            found = missed = 0.0
            for c in cases:
                wanted = [MISSED[label]] if label in MISSED else list(MISSED.values())
                if any(c[w] is None for w in wanted):
                    continue
                mine = [r for r in by_case.get(c["Ref"] or "", []) if label not in MISSED or r["List"] == label]
                found += share(r["Correct? (Y/Partly/N)"] for r in mine).hits
                missed += sum(int(c[w]) for w in wanted)
            recall = Rate(found, found + missed) if found + missed else Rate(0, 0)
            p, rc = precision.value, recall.value
            f1 = round(2 * p * rc / (p + rc), 4) if p and rc else None
            lists.add(
                label,
                len(group),
                precision,
                share((r["Correct? (Y/Partly/N)"] for r in group), partly=None),
                recall,
                f1,
            )
        lists.note = "Partly counts as half (strict: only Y). Recall is estimated: correct items ÷ (correct + missed), over the cases whose missed counts were given."
        report.tables.append(lists)

        quality = Table("Types and quotes", ("List", "Type right", "Quotes support it"))
        for label, group in (*by_list.items(), ("All items", reviewed)):
            kept = [r for r in group if r["Correct? (Y/Partly/N)"] in ("Y", "Partly")]
            quality.add(
                label,
                Rate(sum(1 for r in kept if not r["Right type (if not)"]), len(kept)),
                share(r["Quotes support it? (Y/Partly/N)"] for r in group),
            )
        quality.note = "Type right: of the items judged Y or Partly, those with no Right type given."
        report.tables.append(quality)

        report.tables.append(
            mix(
                (r["Should not be here because"] for r in reviewed if r["Correct? (Y/Partly/N)"] == "N"),
                "Why items shouldn't be there",
                "Reason",
                order=REASONS,
            )
        )

        clean = [c for c in cases if all(c[m] is not None for m in MISSED.values())]
        whole = Table("Whole cases", ("Measure", "Value"))
        whole.add(
            "Cases with every item right and nothing missed",
            Rate(
                sum(
                    1
                    for c in clean
                    if all(int(c[m]) == 0 for m in MISSED.values())
                    and all(r["Correct? (Y/Partly/N)"] == "Y" for r in by_case.get(c["Ref"] or "", []))
                    and by_case.get(c["Ref"] or "")
                ),
                len(clean),
            ),
        )
        report.tables.append(whole)

        auto = Table("From the export (no review needed)", ("Measure", "Value"))
        for header in ("Allegations", "Requests", "Agent statements"):
            mean, mid, _ = spread([float(c[header] or 0) for c in cases])
            auto.add(f"{header} per case: mean", mean)
            auto.add(f"{header} per case: median", mid)
        kept_total = sum(float(c[h] or 0) for c in cases for h in ("Allegations", "Requests", "Agent statements"))
        dropped = sum(float(c["Dropped"] or 0) for c in cases)
        auto.add("Dropped by the code", Rate(dropped, kept_total + dropped))
        auto.add("Marked ambiguous", Rate(sum(1 for r in items if r["Ambiguous"]), len(items)))
        report.tables.append(auto)

        missed_text = Table("What was missed", ("Case no", "What was missed"))
        for c in cases:
            if c["What was missed"]:
                missed_text.add(c["Case no"], c["What was missed"])
        report.texts.append(missed_text)
        return report
