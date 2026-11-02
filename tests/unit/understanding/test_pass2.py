"""C02 Pass 2: deterministic checks, model input, signature, and the stage handler (fake LM)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.contracts import (
    AccountResolution,
    AgentStatement,
    AgentStatementKind,
    Allegation,
    AllegationDetail,
    BaselineAccount,
    BaselineProfile,
    ClaimScope,
    ClaimSetRevision,
    Classification,
    CustomerRequest,
    DetailType,
    GroundingStatus,
    Lane,
    Pass1Result,
    Pass2Issue,
    RequestType,
    SpanRef,
    StatementKind,
    TimeQuality,
)
from cfr_v2.intake import ProfanityFilter, prepare_case
from cfr_v2.intake.fakes import RegexMasker, synthetic_names, synthetic_row
from cfr_v2.llm import FakeModelClient, ModelLoop
from cfr_v2.llm.dspy_lm import gateway_lm
from cfr_v2.registry.evidence_sources import DEFAULT_VERSION, load_taxonomy
from cfr_v2.runtime.stages import STAGES, StageInput
from cfr_v2.understanding import (
    PASS2_VERSION,
    build_claim_set,
    make_pass2_handler,
    make_signature,
    program_inputs,
)

TAXONOMY = load_taxonomy(DEFAULT_VERSION, Path(__file__).resolve().parents[3] / "registry")
SPAN = (SpanRef(interaction_id="i1", start=0, end=5),)
RECEIVED = date(2026, 9, 7)
TRANSCRIPT_REF = "sha256:" + "1" * 64


def _allegation(aid, kind=StatementKind.CLAIM, details=()):
    return Allegation(
        allegation_id=aid,
        text=f"Customer says {aid}",
        kind=kind,
        scope=ClaimScope.THIRD_PARTY,
        spans=SPAN,
        details=details,
    )


PASS1 = Pass1Result(
    case_id="GC-1",
    program_id="c02.pass1",
    program_version="pass1-2",
    transcript_ref=TRANSCRIPT_REF,
    allegations=(
        _allegation(
            "a1",
            details=(AllegationDetail(detail_type=DetailType.AMOUNT, text="412 dollars", span=SPAN[0]),),
        ),
        _allegation("a2", StatementKind.IMPACT),
        _allegation("a3"),
    ),
    requests=(
        CustomerRequest(
            request_id="r1",
            text="Customer asks for a refund",
            request_type=RequestType.REFUND_OR_CREDIT,
            spans=SPAN,
            allegation_ids=("a1",),
        ),
    ),
    agent_statements=(
        AgentStatement(
            statement_id="s1", text="Agent says a dispute is open", kind=AgentStatementKind.ACTION, spans=SPAN
        ),
        AgentStatement(statement_id="s2", text="Agent says the fee is 40", kind=AgentStatementKind.FACT, spans=SPAN),
    ),
)
BASELINE = BaselineProfile(
    query_version="v",
    case_received_date=RECEIVED,
    accounts=(
        BaselineAccount(account_ref="A1", is_case_account=True, product="Gold Card"),
        BaselineAccount(account_ref="A2", is_case_account=False, product="Delta SkyMiles Gold Card"),
    ),
)


def _need(source_type="transactions", origin=None, proposition=None):
    return SimpleNamespace(description="look", source_type=source_type, proposition=proposition, origin=origin)


def _claim(ids, refs=("A1",), about=True, start="2026-09-03", end="2026-09-03", time="EXACT", needs=(), entities=()):
    return SimpleNamespace(
        allegation_ids=list(ids),
        statement="The customer was charged twice",
        about_an_account=about,
        account_refs=list(refs),
        entities=[SimpleNamespace(type=t, value=v) for t, v in entities],
        event_window_start=start,
        event_window_end=end,
        time_quality=time,
        grounding_note=None,
        evidence_needs=list(needs),
    )


def _check(origin, types=("procedure",)):
    return SimpleNamespace(origin=origin, description="check it", proposition="the fee is 40", source_types=list(types))


def _build(claims, checks=(), baseline=BASELINE) -> ClaimSetRevision:
    return build_claim_set(
        PASS1,
        claims,
        checks,
        run_id="run-1",
        baseline=baseline,
        baseline_ref="sha256:" + "2" * 64 if baseline else None,
        received=RECEIVED,
        taxonomy=TAXONOMY,
    )


def _issues(result):
    return [(f.issue, f.ref) for f in result.findings]


# --- deterministic checks --------------------------------------------------------------


def test_a_clean_answer_passes_unchanged():
    result = _build(
        [
            _claim(["a1", "a2"], needs=[_need(), _need("disputes", "s1", "a dispute is open"), _need(origin="r1")]),
            _claim(["a3"], refs=(), about=False, start=None, end=None, time="UNKNOWN"),
        ],
        [_check("s2")],
    )
    assert result.findings == ()
    first, second = result.claims
    assert (first.claim_id, first.allegation_ids, first.account_resolution) == (
        "c1",
        ("a1", "a2"),
        AccountResolution.RESOLVED,
    )
    assert first.grounding_status is GroundingStatus.GROUNDED and first.event_window_start == date(2026, 9, 3)
    assert [n.need_id for n in first.evidence_needs] == ["c1.n1", "c1.n2", "c1.n3"]
    assert second.account_resolution is AccountResolution.NOT_APPLICABLE and second.time_quality is TimeQuality.UNKNOWN
    assert second.grounding_status is GroundingStatus.PARTIAL
    assert result.other_checks[0].origin == "s2" and result.other_checks[0].check_id == "x1"
    assert (result.case_received_date, result.evidence_sources_version) == (RECEIVED, TAXONOMY.version)
    assert result.evidence_sources_hash == TAXONOMY.taxonomy_hash
    assert result.requests == PASS1.requests and result.agent_statements == PASS1.agent_statements


def test_allegations_are_never_lost_or_duplicated():
    result = _build([_claim(["a1", "a2"]), _claim(["a2", "zz"]), _claim(["a1"])])
    assert [c.allegation_ids for c in result.claims] == [("a1", "a2"), ("a3",)]
    added = result.claims[1]
    assert added.statement == "Customer says a3" and added.grounding_status is GroundingStatus.UNGROUNDED
    assert (Pass2Issue.REPEATED_ALLEGATION, "a2") in _issues(result)
    assert (Pass2Issue.EMPTY_CLAIM, "c2") in _issues(result)
    assert (Pass2Issue.UNCOVERED_ALLEGATION, "a3") in _issues(result)


def test_candidate_accounts_keep_the_case_account_first_and_drop_unknown_refs():
    result = _build([_claim(["a1", "a2", "a3"], refs=("A2", "A9", "A1", "A2"))])
    claim = result.claims[0]
    assert claim.account_refs == ("A1", "A2") and claim.account_resolution is AccountResolution.CANDIDATES
    assert claim.grounding_status is GroundingStatus.PARTIAL
    assert (Pass2Issue.UNKNOWN_ACCOUNT_REF, "c1") in _issues(result)


def test_without_a_baseline_no_account_can_be_named():
    result = _build([_claim(["a1", "a2", "a3"], refs=("A1",), time="UNKNOWN", start=None, end=None)], baseline=None)
    claim = result.claims[0]
    assert claim.account_refs == () and claim.account_resolution is AccountResolution.NONE
    assert claim.grounding_status is GroundingStatus.UNGROUNDED
    assert result.baseline_profile_ref is None


@pytest.mark.parametrize(
    ("start", "end", "time", "expected", "issue"),
    [
        ("2026-09-01", "2026-09-10", "APPROXIMATE", (date(2026, 9, 1), RECEIVED, TimeQuality.APPROXIMATE), "AFTER"),
        ("2026-09-10", "2026-09-12", "EXACT", (None, None, TimeQuality.UNKNOWN), "AFTER"),
        ("2026-09-05", "2026-09-01", "EXACT", (None, None, TimeQuality.UNKNOWN), "INVALID"),
        ("3rd of Sept", None, "EXACT", (None, None, TimeQuality.UNKNOWN), "INVALID"),
        ("2026-09-01", "2026-09-02", "UNKNOWN", (date(2026, 9, 1), date(2026, 9, 2), TimeQuality.APPROXIMATE), None),
        (None, None, "EXACT", (None, None, TimeQuality.UNKNOWN), None),
    ],
)
def test_event_windows_are_checked_against_the_received_date(start, end, time, expected, issue):
    result = _build([_claim(["a1", "a2", "a3"], start=start, end=end, time=time)])
    c = result.claims[0]
    assert (c.event_window_start, c.event_window_end, c.time_quality) == expected
    issues = {i.value for i, _ in _issues(result)}
    assert {"AFTER": {"WINDOW_AFTER_RECEIVED"}, "INVALID": {"INVALID_WINDOW"}, None: set()}[issue] <= issues


def test_every_statement_and_request_must_be_checked_somewhere():
    result = _build(
        [_claim(["a1", "a2", "a3"], needs=[_need(origin="s9"), _need("not_a_type", "s1")])],
        [_check("r7")],
    )
    issues = _issues(result)
    assert (Pass2Issue.UNKNOWN_ORIGIN, "s9") in issues and (Pass2Issue.UNKNOWN_ORIGIN, "r7") in issues
    assert (Pass2Issue.UNKNOWN_SOURCE_TYPE, "not_a_type") in issues
    assert {ref for issue, ref in issues if issue is Pass2Issue.NOT_COVERED} == {"r1", "s1", "s2"}
    assert result.claims[0].evidence_needs[0].origin is None and result.other_checks == ()


def test_claim_set_hash_covers_the_new_lists():
    result = _build([_claim(["a1", "a2", "a3"])], [_check("s2")])
    without = ClaimSetRevision.compute_hash(result.allegations, result.claims)
    assert result.claim_set_hash != without


# --- model input and signature -----------------------------------------------------------


def test_model_input_has_texts_and_links_but_no_quotes_or_spans():
    inputs = program_inputs(PASS1, BASELINE, RECEIVED)
    assert inputs["case_received_date"] == "2026-09-07"
    allegations = json.loads(inputs["allegations"])
    assert allegations[0] == {
        "id": "a1",
        "text": "Customer says a1",
        "kind": "CLAIM",
        "about": "third_party",
        "details": [{"type": "amount", "detail": "412 dollars"}],
    }
    assert json.loads(inputs["requests"])[0]["allegations"] == ["a1"]
    assert json.loads(inputs["customer_accounts"])[1] == {
        "account_ref": "A2",
        "is_case_account": False,
        "product": "Delta SkyMiles Gold Card",
    }
    assert "spans" not in json.dumps(inputs) and "quote" not in json.dumps(inputs)
    assert program_inputs(PASS1, None, None)["customer_accounts"] == "none"


def test_signature_lists_active_source_types_and_constrains_the_output():
    sig = make_signature(TAXONOMY)
    for t in TAXONOMY.active():
        assert f"- {t.id}: {t.description}" in sig.instructions
    assert list(sig.input_fields) == [
        "case_received_date",
        "customer_accounts",
        "allegations",
        "requests",
        "agent_statements",
    ]
    assert list(sig.output_fields) == ["claims", "other_checks"]


# --- stage handler -----------------------------------------------------------------------


@pytest.fixture
def loop():
    lp = ModelLoop()
    yield lp
    lp.close()


def _admitted():
    store = InMemoryArtifactStore()
    prepared = prepare_case(
        synthetic_row(0),
        store=store,
        masker=RegexMasker(synthetic_names()),
        profanity=ProfanityFilter(),
        lane=Lane.BATCH,
        received_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    request = prepared.request
    request_ref = store.put(
        request.model_dump_json().encode(), media_type="application/json", classification=Classification.RESTRICTED
    ).metadata.ref
    return store, request, request_ref


def _respond(request):
    return {
        "claims": [
            {
                "allegation_ids": ["a1", "a2", "a3"],
                "statement": "The customer was charged twice",
                "about_an_account": True,
                "account_refs": ["A1"],
                "entities": [{"type": "transaction", "value": "412 dollars"}],
                "event_window_start": None,
                "event_window_end": None,
                "time_quality": "UNKNOWN",
                "grounding_note": "No date given.",
                "evidence_needs": [
                    {"description": "both charges", "source_type": "transactions", "proposition": None, "origin": None},
                    {"description": "refund done", "source_type": "transactions", "proposition": None, "origin": "r1"},
                    {"description": "dispute", "source_type": "disputes", "proposition": "open", "origin": "s1"},
                ],
            }
        ],
        "other_checks": [{"origin": "s2", "description": "fee", "proposition": "40", "source_types": ["procedure"]}],
    }


def _handle(store, request_ref, client, loop, pass1=PASS1):
    pass1_ref = store.put(
        pass1.model_dump_json().encode(), media_type="application/json", classification=Classification.INTERNAL
    ).metadata.ref
    handler = make_pass2_handler(store, gateway_lm(client, "fake-model"), TAXONOMY, loop=loop)
    out = handler(StageInput("run-1", STAGES[1], request_ref, ("p0", pass1_ref), trace_id="t2"))
    return ClaimSetRevision.model_validate_json(out.content)


def test_handler_grounds_with_the_baseline_and_the_received_date(loop):
    store, request, request_ref = _admitted()
    client = FakeModelClient(responder=_respond)
    result = _handle(store, request_ref, client, loop)

    assert STAGES[1].logical_task_id == "c02.pass2" and STAGES[1].program_version == PASS2_VERSION
    assert result.baseline_profile_ref == request.baseline_ref and result.findings == ()
    assert result.case_received_date == synthetic_row(0)["rcpt_ts"].date()  # from the source record
    assert result.claims[0].account_resolution is AccountResolution.RESOLVED

    [model_request] = client.requests
    assert model_request.program_id == "c02.pass2" and model_request.program_version.startswith(PASS2_VERSION)
    assert TAXONOMY.taxonomy_hash[:12] in model_request.program_version
    sent = "".join(t.content for t in model_request.messages)  # the case data (instructions are fixed text)
    row = synthetic_row(0)
    for withheld in (
        *request.case_metadata.values(),  # prim_ctgy / prim_subctgy: not Pass 2 input
        *request.evaluation_labels.values(),
        row["cm15"],  # the ref map stays RESTRICTED
        "⟦PII⟧",  # no transcript text (Pass 1 paraphrases only)
    ):
        assert str(withheld) not in sent
    assert '"account_ref": "A1"' in sent  # baseline accounts are context


def test_handler_without_anything_to_ground_calls_no_model(loop):
    store, _, request_ref = _admitted()
    empty = PASS1.model_copy(update={"allegations": (), "requests": (), "agent_statements": ()})
    client = FakeModelClient(responder=_respond)
    result = _handle(store, request_ref, client, loop, pass1=empty)
    assert result.claims == () and client.requests == []
