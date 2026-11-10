"""Routing one claim: the search, the sections, the model call and the corrections (LDR-0020).

Shared by the worker stage (``handler``) and the trial command, so the two can't drift apart.
Errors are raised: the stage fails on them, the trial records them and goes on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cfr_v2.contracts import ClaimRouting, ClaimRoutingOutcome, ProcedureSearchRequest
from cfr_v2.telemetry import set_attributes, span

from .router import (
    ROUTE_PROGRAM_ID,
    ROUTE_VERSION,
    SEARCH_LIMIT,
    SECTIONS_PER_DOCUMENT,
    ClaimInput,
    Router,
    Section,
    build_routing,
    claim_inputs,
    pick_sections,
)

TASK_ID = "c04.route"


@dataclass(frozen=True)
class RoutedClaim:
    sections: Sequence[Section]
    routing: ClaimRouting
    blocked: bool = False  # the gateway blocked the search, so no sections were shown


def decision(routing: ClaimRouting) -> str:
    """The vertical when routed, otherwise the outcome."""
    if routing.outcome is ClaimRoutingOutcome.ROUTED:
        return routing.candidates[0].vertical_id
    return routing.outcome.value


async def route_claim(
    case_id: str, claim: ClaimInput, search, router: Router, lm, *, trace_id: str | None = None
) -> RoutedClaim:
    from cfr_v2.llm.dspy_lm import ProgramCall, run_program

    with span("c04.route", program=ROUTE_PROGRAM_ID, program_version=ROUTE_VERSION) as route_span:
        found = await search.search(
            ProcedureSearchRequest(
                query=claim.statement, limit=SEARCH_LIMIT, max_per_document=SECTIONS_PER_DOCUMENT, redacted=True
            )
        )
        sections = pick_sections(found.hits)
        call = ProgramCall(
            program_id=ROUTE_PROGRAM_ID,
            program_version=ROUTE_VERSION,
            case_id=case_id,
            task_id=TASK_ID,
            redacted=True,  # claim texts paraphrase redacted text; sections are redacted at snapshot
            trace_id=trace_id,
        )
        prediction = await run_program(
            router,
            lm=lm,
            call=call,
            section_ids=[s.section_id for s in sections],
            **claim_inputs(claim, sections),
        )
        routing = build_routing(claim, sections, prediction.routing, router.catalog)
        candidate = routing.candidates[0] if routing.candidates else None
        set_attributes(
            route_span,
            outcome=decision(routing),
            sections=len(sections),
            sections_cited=len(candidate.procedures) if candidate else 0,
            confidence=candidate.confidence if candidate else None,
            search_blocked=found.blocked,
        )
    return RoutedClaim(sections, routing, found.blocked)
