"""Routing (C04): is each claim's vertical right? (LDR-0033's workbook; LDR-0042 §5.5)

**Workbook** (sheet ``routing``): today's columns unchanged (Q6): Case no, Claim, Primary (Y/N),
Vertical, Reasoning, Sections; for the reviewer, Correct? (Y/N), Correct vertical (if not),
Sections useful? (new), Reason it's wrong. No Ref column: rows are named by Case no + Claim +
Vertical. A claim C04 didn't route shows its outcome (UNMAPPED, NO_ELIGIBLE_VERTICAL) as the
vertical.

**Unit: the claim** (Q14). A case has one or more claims; each claim has one right vertical. A
claim with two routed rows (C04 routes one, so this is rare) is right only if every row is.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from cfr_v2.telemetry import get_logger

from .report import Rate, Report, Table, share
from .source import Reader, Results, Run
from .stage import CASE_NO, ExportContext, stamp
from .workbook import YN, YPN, Column, Problem, Read, Review, Row, Sheet

logger = get_logger("cfr_v2.evaluation.routing")

OUTCOMES = ("UNMAPPED", "NO_ELIGIBLE_VERTICAL")
SHEET = "routing"


def _sections(procedures: Iterable[Mapping[str, Any]]) -> str:
    seen: dict[str, None] = {}
    for p in procedures:
        start, end = p.get("page_start"), p.get("page_end")
        pages = f"p. {start}" if start == end or end is None else f"p. {start}–{end}"
        seen.setdefault(f"{p.get('file_name', '')} · {p.get('section_title', '')} ({pages})")
    return "\n".join(seen)


def claim_rows(
    case_no: str, pass2: Mapping[str, Any] | None, selection: Mapping[str, Any] | None, routing: Mapping[str, Any]
) -> Iterable[tuple[str, ...]]:
    """One row per claim and vertical it was routed to: one vertical per claim, in practice."""
    claims = {c.get("claim_id"): c for c in (pass2 or {}).get("claims", []) if isinstance(c, Mapping)}
    primary = (selection or {}).get("claim_id") if (selection or {}).get("status") == "SELECTED" else None
    for claim in routing.get("claims", []):
        claim_id = claim.get("claim_id")
        statement = (claims.get(claim_id) or {}).get("statement") or claim_id or ""
        is_primary = "Y" if claim_id is not None and claim_id == primary else "N"
        candidates = claim.get("candidates") or []
        if claim.get("outcome") == "ROUTED" and candidates:
            for candidate in candidates:
                reasoning = candidate.get("rationale") or claim.get("rationale") or ""
                yield (
                    case_no,
                    statement,
                    is_primary,
                    candidate.get("vertical_id", ""),
                    reasoning,
                    _sections(candidate.get("procedures") or ()),
                )
        else:
            yield (case_no, statement, is_primary, claim.get("outcome", ""), claim.get("rationale") or "", "")


def release_verticals(registry: Path, release: str, store: Any) -> list[str]:
    """The vertical ids in the knowledge document the release pins; none when they can't be read."""
    from cfr_v2.contracts import AuthorizationContext, Classification
    from cfr_v2.registry import ReleaseRegistry
    from cfr_v2.routing.verticals import VerticalCatalog

    try:
        manifest = ReleaseRegistry(registry).load(release)
        if manifest.routing_verticals is None:
            return []
        reader = AuthorizationContext(principal="console", purpose="routing review: the vertical list")
        raw = store.get(manifest.routing_verticals, classification=Classification.INTERNAL, authorization=reader)
        return list(VerticalCatalog.parse(raw.decode("utf-8")).ids)
    except Exception as exc:  # noqa: BLE001 - the dropdown falls back to the verticals in the rows
        logger.warning("routing review: the release's verticals couldn't be read (%s)", type(exc).__name__)
        return []


class Routing:
    key = "routing"
    title = "Routing (C04)"
    task = "c04.routing"
    tasks = ("c02.pass2", "c03.primary_selection", "c04.routing")
    headline = "Correct vertical"

    def sheets(self, choices: Mapping[str, tuple[str, ...]] | None = None) -> tuple[Sheet, ...]:
        verticals = (choices or {}).get("verticals", ())
        # Exporting without the release's list: offer the verticals the rows were routed to, as before.
        fallback = choices is not None and not verticals
        return (
            Sheet(
                SHEET,
                (
                    CASE_NO,
                    Column("Claim", 60, wrap=True),
                    Column(
                        "Primary (Y/N)",
                        13,
                        note="Y for the claim C03 selected; N for the rest, and for every claim when C03 found it ambiguous",
                    ),
                    Column(
                        "Vertical",
                        28,
                        note="Where C04 routed the claim, or UNMAPPED / NO_ELIGIBLE_VERTICAL when it didn't",
                    ),
                    Column("Reasoning", 70, wrap=True),
                    Column(
                        "Sections", 60, wrap=True, note="The procedure sections the decision relied on, one per line"
                    ),
                    Column(
                        "Correct? (Y/N)",
                        14,
                        review=True,
                        choices=YN,
                        note="For the reviewer: is the vertical right for this claim?",
                    ),
                    Column(
                        "Correct vertical (if not)",
                        28,
                        review=True,
                        choices=OUTCOMES if fallback else verticals,
                        choices_seen="Vertical" if fallback else "",
                        note="For the reviewer: the right one, from the dropdown",
                    ),
                    Column(
                        "Sections useful? (Y/Partly/N)",
                        16,
                        review=True,
                        choices=YPN,
                        optional=True,
                        note="For the reviewer: did the sections cited support the decision?",
                    ),
                    Column(
                        "Reason it's wrong",
                        50,
                        wrap=True,
                        review=True,
                        note="For the reviewer: why the routing is wrong, in a sentence or two",
                    ),
                ),
                key=("Case no", "Claim", "Vertical"),
            ),
        )

    def choices(self, ctx: ExportContext, rows_seen: Iterable[Row]) -> Mapping[str, tuple[str, ...]]:
        verticals = release_verticals(ctx.registry, ctx.release, ctx.store)
        return {"verticals": (*dict.fromkeys(v for v in verticals if v), *OUTCOMES)} if verticals else {}

    def rows(self, run: Run, results: Results, reader: Reader) -> Iterable[Row]:
        routing = results.get(self.task)
        if not isinstance(routing, Mapping):
            return
        stamped = stamp(run, results, self.task)
        for values in claim_rows(run.case_no, results.get("c02.pass2"), results.get("c03.primary_selection"), routing):
            yield Row(SHEET, values, stamped)

    def score(self, review: Review) -> Report:
        rows = review.rows[SHEET]
        reviewed = []
        for r in rows:
            if (
                r["Correct? (Y/N)"] == "N"
                and r["Correct vertical (if not)"]
                and r["Correct vertical (if not)"] == r["Vertical"]
            ):
                review.problems.append(
                    Problem(SHEET, r.line, "Correct? is N, but the correct vertical given is the one it was routed to")
                )
            elif r["Correct? (Y/N)"] is not None:
                reviewed.append(r)
        claims: dict[tuple[str, str], list[Read]] = {}
        for r in reviewed:
            claims.setdefault((r["Case no"] or "", r["Claim"] or ""), []).append(r)
        units = [Claim(group) for group in claims.values()]

        report = Report(self.key, self.headline, Rate(sum(u.correct for u in units), len(units)))
        report.counts = {
            "rows": len(rows),
            "claims reviewed": len(units),
            "rows not reviewed": sum(1 for r in rows if r["Correct? (Y/N)"] is None),
            "claims with two verticals": sum(1 for u in units if len(u.rows) > 1),
        }

        overall = Table("Correct vertical", ("Claims", "Correct vertical"))
        for name, keep in (
            ("All claims", lambda u: True),
            ("Primary claims", lambda u: u.primary),
            ("Other claims", lambda u: not u.primary),
            ("Routed to a vertical", lambda u: u.outcome == "ROUTED"),
            ("UNMAPPED", lambda u: u.outcome == "UNMAPPED"),
            ("NO_ELIGIBLE_VERTICAL", lambda u: u.outcome == "NO_ELIGIBLE_VERTICAL"),
        ):
            group = [u for u in units if keep(u)]
            overall.add(name, Rate(sum(u.correct for u in group), len(group)))
        overall.note = "Counts claims, not cases. A claim is correct when every vertical it was routed to is marked Y."
        report.tables.append(overall)

        verticals = sorted({u.routed for u in units} | {u.right for u in units if u.right})
        per = Table("Per vertical", ("Vertical", "Routed there", "Precision", "Recall"), sortable=True)
        per.note = "Precision: routed there and right. Recall: of the claims whose right vertical it is, how many were routed there. A wrong row with no Correct vertical counts for precision only."
        for v in verticals:
            routed = [u for u in units if u.routed == v]
            belong = [u for u in units if u.right == v]
            per.add(
                v,
                len(routed),
                Rate(sum(u.correct for u in routed), len(routed)),
                Rate(sum(u.routed == v for u in belong), len(belong)),
            )
        report.tables.append(per)

        confusion = Table("Confusions", ("Routed to", "Should be", "Claims"), sortable=True)
        pairs: dict[tuple[str, str], int] = {}
        for u in units:
            if not u.correct and u.right:
                pairs[(u.routed, u.right)] = pairs.get((u.routed, u.right), 0) + 1
        for (routed, right), count in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0])):
            confusion.add(routed, right, count)
        confusion.note = f"{sum(1 for u in units if not u.correct and not u.right)} wrong claims have no Correct vertical, so aren't here."
        report.tables.append(confusion)

        sections = Table("Sections useful?", ("Claims", "Useful"))
        sections.note = "Partly counts as half. Strict counts only Y."
        for name, group in (
            ("All rows", reviewed),
            ("Correct routings", [r for r in reviewed if r["Correct? (Y/N)"] == "Y"]),
            ("Wrong routings", [r for r in reviewed if r["Correct? (Y/N)"] == "N"]),
        ):
            answers = [r["Sections useful? (Y/Partly/N)"] for r in group]
            sections.add(name, share(answers))
        sections.add("All rows, strict", share([r["Sections useful? (Y/Partly/N)"] for r in reviewed], partly=None))
        report.tables.append(sections)

        reasons = Table("Why routings were wrong", ("Case no", "Claim", "Routed to", "Should be", "Reason"))
        for r in reviewed:
            if r["Correct? (Y/N)"] == "N" and r["Reason it's wrong"]:
                reasons.add(
                    r["Case no"], r["Claim"], r["Vertical"], r["Correct vertical (if not)"], r["Reason it's wrong"]
                )
        report.texts.append(reasons)
        return report


class Claim:
    """One claim's reviewed rows: usually one."""

    def __init__(self, rows: Sequence[Read]):
        self.rows = rows
        first = rows[0]
        self.routed = first["Vertical"] or ""
        self.primary = first["Primary (Y/N)"] == "Y"
        self.outcome = self.routed if self.routed in OUTCOMES else "ROUTED"
        self.correct = all(r["Correct? (Y/N)"] == "Y" for r in rows)
        fixes = [
            r["Correct vertical (if not)"]
            for r in rows
            if r["Correct? (Y/N)"] == "N" and r["Correct vertical (if not)"]
        ]
        self.right = self.routed if self.correct else (fixes[0] if fixes else None)
