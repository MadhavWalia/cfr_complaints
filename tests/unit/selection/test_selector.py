"""C03 primary selection (LDR-0018): input projection, shortcuts, corrections, spans, staleness, handler."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.contracts import (
    AccountResolution,
    AgentStatement,
    AgentStatementKind,
    Allegation,
    ClaimScope,
    ClaimSetRevision,
    Classification,
    CustomerRequest,
    EvidenceNeed,
    GroundedClaim,
    GroundingStatus,
    OtherCheck,
    PrimarySelection,
    RequestType,
    SelectionStatus,
    SpanRef,
    StatementKind,
    TimeQuality,
)
from cfr_v2.llm import FakeModelClient, ModelLoop
from cfr_v2.llm.dspy_lm import gateway_lm
from cfr_v2.runtime.stages import STAGES, StageInput
from cfr_v2.selection import (
    SELECT_VERSION,
    build_selection,
    make_selection_handler,
    make_signature,
    selector_inputs,
    shortcut,
)


def _span(start: int) -> tuple[SpanRef, ...]:
    return (SpanRef(interaction_id="i1", start=start, end=start + 5),)


def _allegation(aid, kind, start, about=ClaimScope.SYSTEM, text=None):
    return Allegation(
        allegation_id=aid, text=text or f"Customer says {aid}", kind=kind, scope=about, spans=_span(start)
    )


def _claim(cid, aids, statement):
    return GroundedClaim(
        claim_id=cid,
        allegation_ids=aids,
        statement=statement,
        account_resolution=AccountResolution.NOT_APPLICABLE,
        time_quality=TimeQuality.EXACT,
        evidence_needs=(EvidenceNeed(need_id=f"{cid}.n1", description="EVIDENCE-SECRET", source_type="transactions"),),
        grounding_status=GroundingStatus.GROUNDED,
    )


ALLEGATIONS = (
    _allegation("a1", StatementKind.CLAIM, 0, ClaimScope.AGENT, "Customer says the agent was rude"),
    _allegation("a2", StatementKind.CLAIM, 10, ClaimScope.THIRD_PARTY, "Customer says the hotel charged twice"),
    _allegation("a3", StatementKind.IMPACT, 20, text="IMPACT-SECRET the card was declined at dinner"),
)
CLAIMS = (
    _claim("c1", ("a2", "a3"), "The customer was charged twice by the hotel"),
    _claim("c2", ("a1",), "The customer says an agent was rude"),
)
REQUESTS = (
    CustomerRequest(
        request_id="r1",
        text="Customer asks for a refund",
        request_type=RequestType.REFUND_OR_CREDIT,
        spans=_span(30),
        allegation_ids=("a2",),
    ),
)
STATEMENTS = (
    AgentStatement(statement_id="s1", text="STATEMENT-SECRET", kind=AgentStatementKind.FACT, spans=_span(40)),
)
CHECKS = (OtherCheck(check_id="x1", origin="s1", description="CHECK-SECRET", source_types=("procedure",)),)


def _claim_set(claims=CLAIMS) -> ClaimSetRevision:
    return ClaimSetRevision(
        case_id="GC-1",
        run_id="run-1",
        revision=0,
        allegations=ALLEGATIONS,
        requests=REQUESTS,
        agent_statements=STATEMENTS,
        claims=claims,
        other_checks=CHECKS,
        claim_set_hash=ClaimSetRevision.compute_hash(ALLEGATIONS, claims, REQUESTS, STATEMENTS, CHECKS),
    )


# --- input projection --------------------------------------------------------------------


def test_selector_sees_claims_complaints_order_and_requests_only():
    inputs = selector_inputs(_claim_set())
    claims = json.loads(inputs["claims"])
    # c2's allegation (a1) was raised first, so c2 comes first.
    assert [(c["id"], c["raised"]) for c in claims] == [("c2", 1), ("c1", 2)]
    assert claims[1]["complaints"] == [{"text": "Customer says the hotel charged twice", "about": "third_party"}]
    assert json.loads(inputs["requests"]) == [
        {"text": "Customer asks for a refund", "type": "refund_or_credit", "claims": ["c1"]}
    ]
    sent = json.dumps(inputs)
    for withheld in ("IMPACT-SECRET", "EVIDENCE-SECRET", "STATEMENT-SECRET", "CHECK-SECRET", "spans", "account"):
        assert withheld not in sent


def test_signature_only_allows_this_cases_claim_ids():
    sig = make_signature(["c1", "c2"])
    schema = sig.output_fields["selection"].annotation.model_json_schema()
    assert schema["properties"]["candidate_claim_ids"]["items"]["enum"] == ["c1", "c2"]
    assert "main reason the customer contacted American Express" in sig.instructions


# --- shortcuts and corrections ------------------------------------------------------------


def test_no_model_call_for_zero_or_one_claim():
    empty = _claim_set(claims=())
    assert shortcut(empty).status is SelectionStatus.NO_ELIGIBLE_CLAIM
    one = shortcut(_claim_set(claims=(_claim("c1", ("a1", "a2", "a3"), "s"),)))
    assert (one.status, one.claim_id) == (SelectionStatus.SELECTED, "c1")
    assert shortcut(_claim_set()) is None


def _answer(status, claim_id=None, candidates=(), rationale="Because."):
    return SimpleNamespace(status=status, claim_id=claim_id, candidate_claim_ids=list(candidates), rationale=rationale)


def test_selected_takes_spans_from_the_claims_complaints_not_its_impacts():
    selection = build_selection(_claim_set(), _answer("SELECTED", "c1"))
    assert (selection.status, selection.claim_id) == (SelectionStatus.SELECTED, "c1")
    assert selection.supporting_spans == _span(10)  # a2 (CLAIM), not a3 (IMPACT)
    assert selection.program_version == SELECT_VERSION
    assert selection.claim_set_hash == _claim_set().claim_set_hash


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (_answer("AMBIGUOUS", candidates=("c1", "c2")), (SelectionStatus.AMBIGUOUS, None, ("c1", "c2"))),
        (_answer("AMBIGUOUS", candidates=("c2", "c2")), (SelectionStatus.SELECTED, "c2", ())),  # one candidate
        (_answer("AMBIGUOUS"), (SelectionStatus.AMBIGUOUS, None, ("c1", "c2"))),  # none: every claim
        (_answer("SELECTED", None, ("c1", "c2")), (SelectionStatus.AMBIGUOUS, None, ("c1", "c2"))),
    ],
)
def test_inconsistent_answers_are_corrected_deterministically(answer, expected):
    s = build_selection(_claim_set(), answer)
    assert (s.status, s.claim_id, s.candidate_claim_ids) == expected


def test_third_party_claim_can_be_the_main_complaint():
    assert build_selection(_claim_set(), _answer("SELECTED", "c1")).claim_id == "c1"  # c1 is about the hotel


def test_selection_goes_stale_when_the_claim_set_changes():
    selection = build_selection(_claim_set(), _answer("SELECTED", "c1"))
    merged = _claim_set(claims=(_claim("c1", ("a1", "a2", "a3"), "One complaint"),))
    assert not selection.is_stale(_claim_set().claim_set_hash)
    assert selection.is_stale(merged.claim_set_hash)


# --- stage handler -----------------------------------------------------------------------


@pytest.fixture
def loop():
    lp = ModelLoop()
    yield lp
    lp.close()


def _handle(claim_set, client, loop) -> PrimarySelection:
    store = InMemoryArtifactStore()
    ref = store.put(
        claim_set.model_dump_json().encode(), media_type="application/json", classification=Classification.INTERNAL
    ).metadata.ref
    handler = make_selection_handler(store, gateway_lm(client, "fake-model"), loop=loop)
    out = handler(StageInput("run-1", STAGES[2], "request-ref-unused", ("p1", "p2", ref), trace_id="t3"))
    return PrimarySelection.model_validate_json(out.content)


def test_handler_selects_with_one_model_call_on_the_projection(loop):
    client = FakeModelClient(
        responder=lambda r: {
            "selection": {
                "status": "SELECTED",
                "claim_id": "c1",
                "candidate_claim_ids": [],
                "rationale": "The refund request is about the hotel charges.",
            }
        }
    )
    selection = _handle(_claim_set(), client, loop)
    assert STAGES[2].logical_task_id == "c03.primary_selection" and STAGES[2].program_version == SELECT_VERSION
    assert (selection.status, selection.claim_id) == (SelectionStatus.SELECTED, "c1")
    [request] = client.requests
    assert (request.program_id, request.case_id, request.trace_id) == ("c03.primary_selection", "GC-1", "t3")
    sent = "".join(t.content for t in request.messages)
    assert "IMPACT-SECRET" not in sent and "EVIDENCE-SECRET" not in sent and "STATEMENT-SECRET" not in sent


def test_handler_skips_the_model_for_a_single_claim(loop):
    client = FakeModelClient(responder=lambda r: pytest.fail("no call expected"))
    selection = _handle(_claim_set(claims=(_claim("c1", ("a1", "a2", "a3"), "s"),)), client, loop)
    assert selection.status is SelectionStatus.SELECTED and client.requests == []


# --- trial output -------------------------------------------------------------------------


def test_trial_prints_the_selection_table(capsys):
    from cfr_v2.understanding.__main__ import _Metered, _Outcome, _print_selection

    client = _Metered(inner=None)
    client.usage[("GC-1", 3)] = [900, 300]
    selected = build_selection(_claim_set(), _answer("SELECTED", "c1"))
    ambiguous = build_selection(_claim_set(), _answer("AMBIGUOUS", candidates=("c1", "c2")))
    _print_selection(
        [
            _Outcome("r1", claim_set=_claim_set(), selection=selected, seconds=[1.0, 2.0, 3.0]),
            _Outcome("r2", claim_set=_claim_set(), selection=ambiguous, seconds=[1.0, 2.0, 3.0]),
            _Outcome("r3", claim_set=_claim_set(), seconds=[1.0, 2.0], error="c03: ModelBlockedError"),
            _Outcome("r4"),  # stopped before Pass 2: no row
        ],
        client,
    )
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 5  # title, header, three cases
    assert "selected" in lines[2] and "c1" in lines[2] and "900" in lines[2]
    assert "ambiguous" in lines[3]
    assert "ModelBlockedError" in lines[4]
