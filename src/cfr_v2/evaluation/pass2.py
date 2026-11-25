"""Pass 2 (C02): are the claims right, and how they're grounded? (LDR-0042 §5.3)

**Workbook.** Sheet ``claims``, one row per claim, numbered C1, C2, … in the order the customer
raised them (as Selection numbers them): the claim, the allegations it groups, its account and
event window, grounding, entities, evidence needs and the deterministic corrections; for the
reviewer, Claim correct?, Grouping, Account right?, Event window right?, Evidence needs right?,
Missing evidence source (the release's taxonomy). Sheet ``cases``, one row per case: the other
checks and the case's corrections; for the reviewer, Missed claims (a number) and Other checks
right?. Accounts show as ``A1 · Platinum Card``: the baseline's product, never a number.

**Recall** can only be estimated in a review: claims judged correct ÷ (those + claims missed).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from cfr_v2.console import cases as case_view

from .report import Rate, Report, Table, mix, share, spread
from .selection import ordered_claims
from .source import Reader, Results, Run
from .stage import CASE_NO, NOTES, REF, ExportContext, lines, ref, stamp
from .workbook import YN, YPN, Column, Review, Row, Sheet

CLAIMS, CASES = "claims", "cases"
GROUPING = ("Right", "Should be split", "Should merge with another claim")
ACCOUNT = ("Y", "N", "Not about an account")
WINDOW = ("Y", "N", "Can't tell")
RESOLUTION = {"RESOLVED": "Resolved", "CANDIDATES": "Candidates", "NONE": "None", "NOT_APPLICABLE": "Not applicable"}
CORRECTIONS = {
    "UNCOVERED_ALLEGATION": "An allegation in no claim was given a claim of its own",
    "REPEATED_ALLEGATION": "An allegation in two claims was kept in the first only",
    "EMPTY_CLAIM": "A claim with no known allegations was dropped",
    "UNKNOWN_ACCOUNT_REF": "An account not in the baseline was removed",
    "INVALID_WINDOW": "An unreadable event window was cleared",
    "WINDOW_AFTER_RECEIVED": "The event window was cut back to the received date",
    "UNKNOWN_SOURCE_TYPE": "An evidence need with an unknown source type was dropped",
    "UNKNOWN_ORIGIN": "An unknown origin was removed from an evidence need",
    "NOT_COVERED": "An agent statement or request had no need or check",
}


def taxonomy_ids(ctx: ExportContext) -> list[str]:
    """The active evidence source types of the taxonomy the release pins; none when unreadable."""
    try:
        from cfr_v2.registry import ReleaseRegistry
        from cfr_v2.registry.evidence_sources import load_taxonomy

        manifest = ReleaseRegistry(ctx.registry).load(ctx.release)
        if not manifest.evidence_sources:
            return []
        taxonomy = load_taxonomy(manifest.evidence_sources, ctx.registry / "evidence_sources")
        return [t.id for t in taxonomy.active()]
    except Exception:  # noqa: BLE001 - the dropdown falls back to the source types in the rows
        return []


class Pass2:
    key = "pass2"
    title = "Pass 2 (C02)"
    task = "c02.pass2"
    tasks = ("c02.pass2",)
    headline = "Claim precision"

    def sheets(self, choices: Mapping[str, tuple[str, ...]] | None = None) -> tuple[Sheet, ...]:
        sources = (choices or {}).get("sources", ())
        return (
            Sheet(
                CLAIMS,
                (
                    CASE_NO,
                    Column("Claim no", 9, note="C1, C2, … in the order the customer raised them"),
                    Column("Claim", 60, wrap=True),
                    Column("Allegations", 60, wrap=True, note="The Pass 1 allegations the claim groups, one per line"),
                    Column(
                        "Account",
                        16,
                        note="Resolved (one account), Candidates (several could fit), None, or Not applicable (not about an account)",
                    ),
                    Column(
                        "Accounts",
                        30,
                        wrap=True,
                        note="The accounts named, as the baseline describes them; never a number",
                    ),
                    Column("Event window", 24),
                    Column("Time quality", 14, note='Exact (stated dates), Approximate ("last week") or Unknown'),
                    Column("Grounding", 24),
                    Column("Grounding note", 40, wrap=True),
                    Column("Entities", 30, wrap=True),
                    Column(
                        "Evidence needs",
                        60,
                        wrap=True,
                        note="What an investigator would check: source type, then what, one per line",
                    ),
                    Column(
                        "Corrections", 40, wrap=True, note="What the code corrected in Pass 2's answer for this claim"
                    ),
                    Column(
                        "Claim correct? (Y/Partly/N)",
                        16,
                        review=True,
                        choices=YPN,
                        note="For the reviewer: is it a real claim the customer made, stated faithfully?",
                    ),
                    Column(
                        "Grouping",
                        22,
                        review=True,
                        choices=GROUPING,
                        note="For the reviewer: are the right allegations grouped into this claim?",
                    ),
                    Column("Account right?", 16, review=True, choices=ACCOUNT),
                    Column("Event window right?", 16, review=True, choices=WINDOW),
                    Column("Evidence needs right? (Y/Partly/N)", 18, review=True, choices=YPN),
                    Column(
                        "Missing evidence source",
                        26,
                        review=True,
                        choices=sources,
                        note="For the reviewer: a source type an investigator would need that isn't listed",
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
                    Column("Claims", 9),
                    Column(
                        "Other checks",
                        60,
                        wrap=True,
                        note="Agent statements or requests linked to no claim, still checked",
                    ),
                    Column(
                        "Case corrections", 40, wrap=True, note="What the code corrected that isn't about one claim"
                    ),
                    Column(
                        "Missed claims",
                        12,
                        review=True,
                        number=True,
                        note="For the reviewer: how many claims the customer made that aren't listed (0 when none)",
                    ),
                    Column("Other checks right?", 14, review=True, choices=YN),
                    NOTES,
                    REF,
                ),
                key=("Ref",),
            ),
        )

    def choices(self, ctx: ExportContext, rows_seen: Iterable[Row]) -> Mapping[str, tuple[str, ...]]:
        ids = taxonomy_ids(ctx)
        return {"sources": tuple(ids)} if ids else {}

    def rows(self, run: Run, results: Results, reader: Reader) -> Iterable[Row]:
        pass2 = results.get(self.task)
        if not isinstance(pass2, Mapping):
            return
        stamped = stamp(run, results, self.task)
        customer = case_view.customer_data(pass2, reader.baseline(results))
        allegations = {a.get("allegation_id"): a for a in pass2.get("allegations") or ()}
        claims = ordered_claims(pass2)
        findings = [f for f in pass2.get("findings") or () if isinstance(f, Mapping)]
        ids = {c.get("claim_id") for c in claims}
        for i, claim in enumerate(claims, start=1):
            claim_id = claim.get("claim_id")
            start, end = claim.get("event_window_start"), claim.get("event_window_end")
            window = f"{start or '?'} – {end or '?'}" if start or end else ""
            yield Row(
                CLAIMS,
                (
                    run.case_no,
                    f"C{i}",
                    claim.get("statement", ""),
                    lines(
                        f"{a} {allegations.get(a, {}).get('text', '')}".strip()
                        for a in claim.get("allegation_ids") or ()
                    ),
                    RESOLUTION.get(claim.get("account_resolution", ""), claim.get("account_resolution", "")),
                    lines(customer.label(r) for r in claim.get("account_refs") or ()),
                    window,
                    (claim.get("time_quality") or "").capitalize(),
                    claim.get("grounding_status", ""),
                    claim.get("grounding_note") or "",
                    lines(f"{e.get('entity_type', '')}: {e.get('reference', '')}" for e in claim.get("entities") or ()),
                    lines(_need(n) for n in claim.get("evidence_needs") or ()),
                    lines(
                        CORRECTIONS.get(f.get("issue", ""), f.get("issue", ""))
                        for f in findings
                        if f.get("ref") == claim_id
                    ),
                    ref(run, claim_id),
                ),
                stamped,
            )
        yield Row(
            CASES,
            (
                run.case_no,
                len(claims),
                lines(_check(c) for c in pass2.get("other_checks") or ()),
                lines(
                    CORRECTIONS.get(f.get("issue", ""), f.get("issue", "")) for f in findings if f.get("ref") not in ids
                ),
                ref(run),
            ),
            stamped,
        )

    def score(self, review: Review) -> Report:
        claims, cases = review.rows[CLAIMS], review.rows[CASES]
        reviewed = [r for r in claims if r["Claim correct? (Y/Partly/N)"] is not None]
        precision = share(r["Claim correct? (Y/Partly/N)"] for r in reviewed)
        report = Report(self.key, self.headline, precision)
        report.counts = {
            "claims": len(claims),
            "claims reviewed": len(reviewed),
            "claims not reviewed": len(claims) - len(reviewed),
            "cases": len(cases),
            "cases with missed claims given": sum(1 for c in cases if c["Missed claims"] is not None),
        }

        # Recall over the cases whose Missed claims was filled in, from their reviewed claims only.
        by_case = {}
        for r in reviewed:
            by_case.setdefault(r["Ref"].split("/")[0] if r["Ref"] else r["Case no"], []).append(r)
        found = missed = 0.0
        for c in cases:
            if c["Missed claims"] is None:
                continue
            mine = by_case.get(c["Ref"] or c["Case no"], [])
            found += share(r["Claim correct? (Y/Partly/N)"] for r in mine).hits
            missed += int(c["Missed claims"])
        recall = Rate(found, found + missed) if found + missed else Rate(0, 0)

        main = Table("Claims", ("Measure", "Value"))
        main.add("Claim precision", precision)
        main.add("Claim precision, strict", share((r["Claim correct? (Y/Partly/N)"] for r in reviewed), partly=None))
        main.add("Estimated claim recall", recall)
        grouping = [r["Grouping"] for r in reviewed if r["Grouping"]]
        main.add("Grouping right", Rate(grouping.count("Right"), len(grouping)))
        main.add("Should be split", grouping.count("Should be split"))
        main.add("Should merge with another claim", grouping.count("Should merge with another claim"))
        checks = [c["Other checks right?"] for c in cases]
        main.add("Other checks right", share(checks, partly=None))
        main.note = "Partly counts as half (strict: only Y). Recall is estimated: correct claims ÷ (correct + missed), over the cases whose Missed claims was given."
        report.tables.append(main)

        account = Table("Account right?, by resolution", ("Account", "Claims", "Right"))
        for resolution in RESOLUTION.values():
            group = [
                r["Account right?"]
                for r in reviewed
                if r["Account"] == resolution and r["Account right?"] in ("Y", "N")
            ]
            account.add(resolution, len(group), Rate(group.count("Y"), len(group)))
        account.note = "Not about an account answers are left out."
        report.tables.append(account)

        window = Table("Event window right?, by time quality", ("Time quality", "Claims", "Right"))
        for quality in ("Exact", "Approximate", "Unknown"):
            group = [
                r["Event window right?"]
                for r in reviewed
                if r["Time quality"] == quality and r["Event window right?"] in ("Y", "N")
            ]
            window.add(quality, len(group), Rate(group.count("Y"), len(group)))
        window.note = "Can't tell answers are left out."
        report.tables.append(window)

        needs = Table("Evidence needs", ("Measure", "Value"))
        needs.add("Evidence needs right", share(r["Evidence needs right? (Y/Partly/N)"] for r in reviewed))
        needs.add(
            "Evidence needs right, strict",
            share((r["Evidence needs right? (Y/Partly/N)"] for r in reviewed), partly=None),
        )
        report.tables.append(needs)
        report.tables.append(
            mix((r["Missing evidence source"] for r in reviewed), "Most missed evidence sources", "Source type")
        )

        sizes = [float(c["Claims"] or 0) for c in cases]
        mean, mid, p90 = spread(sizes)
        auto = Table("From the export (no review needed)", ("Measure", "Value"))
        auto.add("Claims per case: mean", mean)
        auto.add("Claims per case: median", mid)
        auto.add("Claims per case: 90th percentile", p90)
        report.tables.append(auto)
        report.tables.append(mix((r["Grounding"] for r in claims), "Grounding status", "Status"))
        report.tables.append(
            mix((r["Account"] for r in claims), "Account resolution", "Account", order=list(RESOLUTION.values()))
        )
        report.tables.append(
            mix(
                (r["Time quality"] for r in claims),
                "Time quality",
                "Time quality",
                order=("Exact", "Approximate", "Unknown"),
            )
        )
        corrections = [line for r in claims for line in (r["Corrections"] or "").splitlines()]
        corrections += [line for c in cases for line in (c["Case corrections"] or "").splitlines()]
        report.tables.append(mix(corrections, "Corrections the code made", "Correction"))
        return report


def _need(need: Mapping[str, Any]) -> str:
    text = f"{need.get('source_type', '')}: {need.get('description', '')}"
    return text + (f" ({need['proposition']})" if need.get("proposition") else "")


def _check(check: Mapping[str, Any]) -> str:
    sources = ", ".join(check.get("source_types") or ())
    return f"{check.get('origin', '')} {check.get('description', '')} [{sources}]".strip()
