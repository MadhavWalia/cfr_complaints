"""C04 router (LDR-0019): vertical definitions, section choice, inputs, corrections and the trial."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from cfr_v2.contracts import ClaimRoutingOutcome, ProcedureHit, ProcedureSearchResult
from cfr_v2.llm import FakeModelClient, ModelBlockedError
from cfr_v2.llm.dspy_lm import gateway_lm
from cfr_v2.routing import (
    ClaimInput,
    Router,
    VerticalCatalog,
    VerticalDocumentError,
    build_routing,
    claim_inputs,
    make_signature,
    pick_sections,
)
from cfr_v2.routing import __main__ as trial

DOCUMENT = """## KNOWLEDGE DOCUMENT

## VERTICAL INDEX — KEYWORD ROUTING

**NEW ACCOUNTS:** application lost, decline decision

**RLA (RESPONSIBLE LENDING ACTIONS):** line reduction, global limit

**HOLD:** payment hold, payment posting error

**UNMAPPED OPERATIONAL RISK:** taxonomy gap

## VERTICAL RULES

### HOLD
**Op-Risk principle:** RULES-MARKER payments are held only as the procedure says.
"""
CATALOG = VerticalCatalog.parse(DOCUMENT)
INDEX = "sha256:" + "a" * 64


def _hit(doc: int, section: int, rank: int) -> ProcedureHit:
    return ProcedureHit(
        procedure_name=f"Procedure {doc}",
        file_name=f"Procedure {doc}.pdf",
        source_sha256=f"{doc:x}" * 64,
        section_index=section,
        section_title=f"{doc}.{section} Section",
        part=1,
        page_start=section,
        page_end=section + 1,
        text=f"SECTION-TEXT {doc}.{section}",
        score=1 / rank,
        vector_rank=rank,
    )


def test_the_verticals_are_the_index_entries_less_unmapped():
    assert CATALOG.ids == ("NEW ACCOUNTS", "RLA", "HOLD")
    assert CATALOG.verticals[1].full_name == "RESPONSIBLE LENDING ACTIONS"
    assert re.fullmatch(r"[0-9a-f]{64}", CATALOG.sha256)


@pytest.mark.parametrize(
    "text",
    [
        "no index here",
        "## VERTICAL INDEX\n\n**UNMAPPED OPERATIONAL RISK:** gap\n",
        "## VERTICAL INDEX\n**A:** x\n**A:** y",
    ],
)
def test_a_document_without_usable_verticals_is_refused(text):
    with pytest.raises(VerticalDocumentError):
        VerticalCatalog.parse(text)


def test_the_best_two_sections_of_the_six_best_documents_in_search_order():
    order = [(1, 1), (2, 1), (1, 2), (1, 3), (3, 1), (4, 1), (5, 1), (6, 1), (7, 1), (2, 2), (7, 2)]
    hits = [_hit(d, s, rank) for rank, (d, s) in enumerate(order, start=1)]
    sections = pick_sections(hits)
    assert [(s.section_id, s.hit.section_title) for s in sections] == [
        ("S1", "1.1 Section"),
        ("S2", "1.2 Section"),
        ("S3", "2.1 Section"),
        ("S4", "2.2 Section"),
        ("S5", "3.1 Section"),
        ("S6", "4.1 Section"),
        ("S7", "5.1 Section"),
        ("S8", "6.1 Section"),
    ]


def test_the_router_sees_the_statement_and_claim_complaints_only():
    allegations = {
        "a1": {"allegation_id": "a1", "text": "Customer says the payment was held", "kind": "CLAIM"},
        "a2": {"allegation_id": "a2", "text": "IMPACT-SECRET rent was late", "kind": "IMPACT"},
    }
    claim = ClaimInput.from_review(
        {"claim_id": "c1", "statement": "A payment was held too long", "allegation_ids": ["a1", "a2"]}, allegations
    )
    inputs = claim_inputs(claim, pick_sections([_hit(1, 1, 1)]))
    assert json.loads(inputs["claim"]) == {
        "statement": "A payment was held too long",
        "complaints": ["Customer says the payment was held"],
    }
    assert "IMPACT-SECRET" not in json.dumps(inputs)
    assert json.loads(inputs["sections"])[0] == {
        "id": "S1",
        "procedure": "Procedure 1",
        "section": "1.1 Section",
        "pages": "1-2",
        "text": "SECTION-TEXT 1.1",
    }


def test_the_answer_can_only_name_known_verticals_and_shown_sections():
    schema = make_signature(CATALOG, ["S1", "S2"]).output_fields["routing"].annotation.model_json_schema()
    text = json.dumps(schema)
    assert all(v in text for v in CATALOG.ids) and '"S1"' in text and "UNMAPPED OPERATIONAL RISK" not in text
    assert make_signature(CATALOG, ["S1"]).instructions.endswith(DOCUMENT.strip())  # DSPy trims it


def _answer(**kw):
    base = {
        "outcome": "ROUTED",
        "vertical": "HOLD",
        "reason": "A held payment.",
        "section_ids": [],
        "confidence": "high",
    }
    return SimpleNamespace(**{**base, **kw})


CLAIM = ClaimInput("c1", "A payment was held too long")
SECTIONS = pick_sections([_hit(1, 1, 1), _hit(2, 4, 2)])


def test_a_routed_claim_keeps_its_reason_and_the_sections_it_relied_on():
    routing = build_routing(CLAIM, SECTIONS, _answer(section_ids=["S2", "S2", "S9"]), CATALOG)
    assert routing.outcome is ClaimRoutingOutcome.ROUTED and routing.rationale == "A held payment."
    [route] = routing.candidates
    assert (route.vertical_id, route.confidence, route.rationale) == ("HOLD", 0.9, "A held payment.")
    assert [(c.file_name, c.section_index) for c in route.procedures] == [("Procedure 2.pdf", 4)]
    assert len(routing.sections_seen) == 2


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (_answer(vertical=None), ClaimRoutingOutcome.UNMAPPED),
        (_answer(vertical="NOT A VERTICAL"), ClaimRoutingOutcome.UNMAPPED),
        (_answer(outcome="NO_ELIGIBLE_VERTICAL"), ClaimRoutingOutcome.NO_ELIGIBLE_VERTICAL),
        (_answer(outcome="UNMAPPED", reason=" "), ClaimRoutingOutcome.UNMAPPED),
    ],
)
def test_corrections_are_deterministic(answer, outcome):
    routing = build_routing(CLAIM, SECTIONS, answer, CATALOG)
    assert routing.outcome is outcome and routing.candidates == () and routing.rationale


class Search:
    def __init__(self, hits=(), error=None):
        self.hits, self.error, self.requests = hits, error, []

    async def search(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return ProcedureSearchResult(index_ref=INDEX, vector_search=True, hits=tuple(self.hits))


def _router_reply(request):
    return {
        "routing": {
            "outcome": "ROUTED",
            "vertical": "HOLD",
            "reason": "The claim is about a payment hold, which HOLD owns; S1 sets out the hold period.",
            "section_ids": ["S1"],
            "confidence": "medium",
        }
    }


async def test_one_claim_is_searched_then_routed_with_one_model_call():
    client = FakeModelClient(responder=_router_reply)
    search = Search([_hit(1, 1, 1), _hit(2, 1, 2)])
    routed = await trial.route_claim("GC-1", CLAIM, search, Router(CATALOG), gateway_lm(client, "fake-model"))
    [query] = search.requests
    assert (query.query, query.redacted, query.max_per_document) == (CLAIM.statement, True, 2)
    assert routed.decision == "HOLD" and routed.error is None
    assert routed.routing.candidates[0].procedures[0].file_name == "Procedure 1.pdf"
    [request] = client.requests
    assert (request.program_id, request.case_id, request.task_id) == ("c04.route", "GC-1", "c04.route")
    sent = request.instructions + "".join(t.content for t in request.messages)
    assert "RULES-MARKER" in sent and "SECTION-TEXT 2.1" in sent


async def test_a_failed_claim_is_recorded_and_the_rest_go_on():
    client = FakeModelClient(responder=_router_reply)
    cases = [trial.CaseClaims("GC-1", [CLAIM]), trial.CaseClaims("GC-2", [ClaimInput("c1", "Another")])]

    class Flaky(Search):
        async def search(self, request):
            if request.query == "Another":
                raise ModelBlockedError("blocked")
            return await super().search(request)

    results = await trial.route_all(cases, Flaky([_hit(1, 1, 1)]), Router(CATALOG), gateway_lm(client, "m"), 2)
    assert [(r.case_id, r.decision, r.error) for r in results] == [
        ("GC-1", "HOLD", None),
        ("GC-2", "FAILED", "ModelBlockedError"),
    ]


def test_claims_are_read_from_c02_review_files(tmp_path):
    review = {
        "case_id": "GC-1",
        "allegations": [{"allegation_id": "a1", "text": "Customer says X", "kind": "CLAIM"}],
        "claims": [{"claim_id": "c1", "statement": "X happened", "allegation_ids": ["a1"]}],
    }
    (tmp_path / "GC-1.json").write_text(json.dumps(review))
    (tmp_path / "GC-2.json").write_text(json.dumps({"case_id": "GC-2", "claims": []}))
    [case] = trial.load_cases(tmp_path)
    assert (case.case_id, case.claims) == ("GC-1", [ClaimInput("c1", "X happened", ("Customer says X",))])


async def test_review_files_workbook_and_summary(tmp_path, capsys):
    from openpyxl import load_workbook

    client = FakeModelClient(responder=_router_reply)
    routed = await trial.route_claim("GC/1", CLAIM, Search([_hit(1, 1, 1)]), Router(CATALOG), gateway_lm(client, "m"))
    failed = trial.Routed("GC-2", ClaimInput("c1", "Other"), error="ModelBlockedError")
    trial.write_reviews([routed, failed], tmp_path, CATALOG, INDEX)
    review = json.loads((tmp_path / "GC_1.json").read_text())
    [claim] = review["claims"]
    assert (claim["routed_to"], review["verticals_sha256"]) == ("HOLD", CATALOG.sha256)
    assert claim["routing"]["rationale"].startswith("The claim is about a payment hold")
    assert claim["sections_shown"] == [
        {"id": "S1", "file_name": "Procedure 1.pdf", "section": "1.1 Section", "pages": "1-2"}
    ]
    trial.write_workbook([routed, failed], tmp_path / "routing.xlsx", CATALOG)
    sheet = load_workbook(tmp_path / "routing.xlsx")["routing"]
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0] == trial.REVIEW_HEADERS
    assert rows[1][4:8] == ("HOLD", "medium", claim["routing"]["rationale"], "Procedure 1.pdf: 1.1 Section (pp. 1-2)")
    assert rows[2][4:7] == ("FAILED", None, "ModelBlockedError")  # an empty cell reads back as None
    choices = [dv.formula1 for dv in sheet.data_validations.dataValidation]
    assert '"Yes,No"' in choices and any("NO_ELIGIBLE_VERTICAL" in c and "RLA" in c for c in choices)
    metered = trial.Metered(client)
    metered.calls, metered.input_tokens, metered.output_tokens = 1, 2000, 100
    trial.print_summary([routed, failed], metered, search_calls=1, seconds=3)
    out = capsys.readouterr().out
    assert "2 claims routed in 3s; 1 failed" in out and "HOLD" in out and "medium 1" in out
    assert "payment hold" not in out and "Other" not in out  # numbers only
