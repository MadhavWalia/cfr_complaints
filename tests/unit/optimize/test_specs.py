"""Gold specs for every prompt (LDR-0027 phase 2): templates with frozen inputs, reading filled
workbooks back into examples, the checks a workbook must pass, and each metric."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from cfr_v2.contracts import (
    ClaimRoutingOutcome,
    Pass1Result,
    ProcedureCitation,
    RoutingResult,
)
from cfr_v2.contracts.routing import ClaimRouting
from cfr_v2.llm import FakeModelClient
from cfr_v2.optimize import session
from cfr_v2.optimize.prompts import BY_ID
from cfr_v2.optimize.spec_pass1 import SPEC as PASS1
from cfr_v2.optimize.spec_pass2 import SPEC as PASS2
from cfr_v2.optimize.spec_pass2 import GoldClaim
from cfr_v2.optimize.spec_routing import SPEC as ROUTING
from cfr_v2.optimize.spec_selection import SPEC as SELECTION
from cfr_v2.optimize.specs import SPECS
from cfr_v2.registry.evidence_sources import load_taxonomy
from cfr_v2.selection.selector import selector_inputs
from cfr_v2.understanding.pass2 import program_inputs
from tests.unit.routing.test_router import CATALOG
from tests.unit.selection.test_selector import ALLEGATIONS, REQUESTS, STATEMENTS, _claim_set

REPO = Path(__file__).resolve().parents[3]
CLAIM_SET = _claim_set()
PASS1_RESULT = Pass1Result(
    case_id="GC-1", program_id="c02.pass1", program_version="pass1-2", transcript_ref="sha256:" + "1" * 64,
    allegations=ALLEGATIONS, requests=REQUESTS, agent_statements=STATEMENTS,
)  # fmt: skip
SNAPSHOT = "sha256:" + "a" * 64


def _passage(doc: int, section: int):
    document = SimpleNamespace(
        name=f"Procedure {doc}", file_name=f"Procedure {doc}.pdf", source_sha256=f"{doc:x}" * 64,
        annual_review_date=None, monthly_revision_date=None,
    )  # fmt: skip
    return SimpleNamespace(
        document=document,
        section=SimpleNamespace(index=section, title=f"{doc}.{section} Section", part=1, page_start=section,
                                page_end=section + 1, text=f"SECTION-TEXT {doc}.{section}"),
    )  # fmt: skip


PASSAGES = {(p.document.source_sha256, p.section.index, 1): p for p in (_passage(1, 1), _passage(2, 4))}


def _citation(p) -> ProcedureCitation:
    return ProcedureCitation(source_sha256=p.document.source_sha256, file_name=p.document.file_name,
                             section_index=p.section.index, part=1, section_title=p.section.title,
                             page_start=p.section.page_start, page_end=p.section.page_end)  # fmt: skip


ROUTING_RESULT = RoutingResult(
    claim_set_hash=CLAIM_SET.claim_set_hash, program_id="c04.routing", program_version="route-1",
    procedures_ref=SNAPSHOT,
    claims=(
        ClaimRouting(claim_id="c1", outcome=ClaimRoutingOutcome.UNMAPPED,
                     sections_seen=tuple(_citation(p) for p in PASSAGES.values())),
        ClaimRouting(claim_id="c2", outcome=ClaimRoutingOutcome.NO_ELIGIBLE_VERTICAL),
    ),
)  # fmt: skip


class FakeContext:
    """Every case number has a run; committed results come from ``results`` by task."""

    def __init__(self, results=None, *, missing=()):
        self.results = results or {
            "c02.pass1": (PASS1_RESULT, "ref-pass1"),
            "c02.pass2": (CLAIM_SET, "ref-claims"),
            "c04.routing": (ROUTING_RESULT, "ref-routing"),
        }
        self.missing = set(missing)
        self.taxonomy = load_taxonomy("evidence-sources-1", REPO / "registry")
        self.catalog = CATALOG
        self.store = None

    def runs(self, case_nos):
        return {c: (f"run-{c}", f"req-{c}", "rel@1") for c in case_nos if c not in self.missing}

    def committed(self, run_id, task, model):
        return self.results.get(task, (None, None))

    def request(self, ref):
        return SimpleNamespace(baseline_ref=None, source_record_ref=None, transcript_ref="sha256:" + "1" * 64)

    def passages(self, ref):
        assert ref == SNAPSHOT
        return PASSAGES


def _fill(path: Path, sheet: str, rows: dict[str, list]) -> None:
    """Write answers the way a labeller would: by column name, on the given (1-based data) rows."""
    book = load_workbook(path)
    ws = book[sheet]
    header = [c.value for c in ws[1]]
    for row_no, values in rows.items():
        for column, value in values:
            ws.cell(row=int(row_no) + 1, column=header.index(column) + 1, value=value)
    book.save(path)


def test_every_prompt_has_a_spec():
    assert set(SPECS) == set(BY_ID)


# --- C03 -------------------------------------------------------------------------------------


def test_c03_template_reads_back_as_the_stages_own_inputs(tmp_path):
    ctx = FakeContext()
    path = tmp_path / "c03.xlsx"
    assert SELECTION.template(["GC-1", "GC-2"], path, ctx) == []
    book = load_workbook(path)
    assert book.sheetnames == ["read me", "_lists", "cases", "claims", "requests", "_frozen"]
    assert book["claims"].protection.sheet and book["cases"]["C2"].protection.locked is False  # context locked
    assert "keep it on this machine" in book["read me"]["A3"].value.lower()
    _fill(path, "cases", {"1": [("primary_claim_no", 1)], "2": [("unclear", "Y")]})
    gold = SELECTION.read([path], ctx)
    assert gold.problems == [] and [e.case_id for e in gold.examples] == ["GC-1", "GC-2"]
    first = gold.examples[0]
    assert first.inputs == selector_inputs(CLAIM_SET) | {"claim_ids": ["c2", "c1"]}  # as raised
    assert (first.label.primary, gold.examples[1].label.unclear) == ("c2", True)


def test_c03_checks_the_answers_and_the_frozen_inputs(tmp_path):
    ctx = FakeContext()
    path = tmp_path / "c03.xlsx"
    SELECTION.template(["GC-1", "GC-2", "GC-3"], path, ctx)
    _fill(path, "cases", {"1": [("primary_claim_no", 7)], "2": [("primary_claim_no", 1), ("unclear", "Y")],
                          "3": [("primary_claim_no", 2)]})  # fmt: skip
    gold = SELECTION.read([path], ctx)
    assert [e.case_id for e in gold.examples] == ["GC-3"]
    assert any("GC-1" in p and "one of 1 to 2" in p for p in gold.problems)
    assert any("GC-2" in p and "choose one" in p for p in gold.problems)
    changed = FakeContext({"c02.pass2": (CLAIM_SET, "ref-newer")})
    assert any("changed since the template" in p for p in SELECTION.read([path], changed).problems)
    one = FakeContext({"c02.pass2": (_claim_set(CLAIM_SET.claims[:1]), "ref")})
    assert any("without the model" in p for p in SELECTION.template(["GC-9"], tmp_path / "one.xlsx", one))


def _selection(status, claim_id=None, candidates=()):
    return SimpleNamespace(selection=SimpleNamespace(status=status, claim_id=claim_id, candidate_claim_ids=candidates))


def test_c03_metric():
    from cfr_v2.optimize.spec_selection import Label

    clear, unclear = Label("c1", False, ("c1", "c2")), Label(None, True, ("c1", "c2"))
    assert SELECTION.metric(clear, _selection("SELECTED", "c1")).score == 1.0
    wrong = SELECTION.metric(clear, _selection("SELECTED", "c2"))
    assert wrong.score == 0.0 and wrong.feedback == "Chose claim 2; the main complaint is claim 1."
    assert SELECTION.metric(clear, _selection("AMBIGUOUS", None, ["c1", "c2"])).score == 0.25
    assert SELECTION.metric(unclear, _selection("AMBIGUOUS", None, ["c1", "c2"])).score == 1.0
    assert SELECTION.metric(unclear, _selection("SELECTED", "c1")).score == 0.0


# --- Pass 2 ----------------------------------------------------------------------------------


def test_pass2_template_and_reading_the_claims(tmp_path):
    ctx = FakeContext()
    path = tmp_path / "pass2.xlsx"
    assert PASS2.template(["GC-1", "GC-2"], path, ctx) == []
    book = load_workbook(path)
    assert [r[2].value for r in book["allegations"].iter_rows(min_row=2)][:1] == [ALLEGATIONS[0].text]
    assert book["allegations"].protection.sheet and not book["claims"].protection.sheet  # claims: add rows
    claims = [
        [("case_no", "GC-1"), ("claim_no", 1), ("allegation_nos", "2, 3"), ("about_an_account", "N"),
         ("event_window_start", "2026-09-03"), ("event_window_end", "2026-09-03"), ("time_quality", "EXACT"),
         ("evidence_1", "transactions")],
        [("case_no", "GC-1"), ("claim_no", 2), ("allegation_nos", "1"), ("about_an_account", "N"),
         ("time_quality", "UNKNOWN")],
        [("case_no", "GC-2"), ("claim_no", 1), ("allegation_nos", "1, 9"), ("about_an_account", "N"),
         ("time_quality", "UNKNOWN")],
    ]  # fmt: skip
    _fill(path, "claims", {str(i): row for i, row in enumerate(claims, start=1)})
    gold = PASS2.read([path], ctx)
    assert [e.case_id for e in gold.examples] == ["GC-1"]
    assert any("GC-2" in p and "isn't one of the case's 1 to 3" in p for p in gold.problems)
    example = gold.examples[0]
    assert example.inputs == program_inputs(PASS1_RESULT, None, None)
    assert example.label[0] == GoldClaim(frozenset({"a2", "a3"}), False, ("2026-09-03", "2026-09-03"), "EXACT",
                                         frozenset({"transactions"}))  # fmt: skip


def _claim(ids, about=False, start=None, end=None, quality="UNKNOWN", sources=()):
    needs = [SimpleNamespace(source_type=s) for s in sources]
    return SimpleNamespace(allegation_ids=list(ids), about_an_account=about, event_window_start=start,
                           event_window_end=end, time_quality=quality, evidence_needs=needs)  # fmt: skip


def test_pass2_metric():
    label = (
        GoldClaim(frozenset({"a2", "a3"}), False, ("2026-09-03", "2026-09-03"), "EXACT", frozenset({"transactions"})),
        GoldClaim(frozenset({"a1"}), False, (None, None), "UNKNOWN", frozenset()),
    )
    right = [_claim(["a2", "a3"], start="2026-09-03", end="2026-09-03", quality="EXACT", sources=["transactions"]),
             _claim(["a1"])]  # fmt: skip
    assert PASS2.metric(label, SimpleNamespace(claims=right)).score == 1.0
    merged = PASS2.metric(label, SimpleNamespace(claims=[_claim(["a1", "a2", "a3"])]))
    assert merged.score < 0.5 and "1 of 2 matched" in merged.feedback  # it overlaps one well enough
    sources = PASS2.metric(label, SimpleNamespace(claims=[_claim(["a2", "a3"], start="2026-09-03",
                                                                 end="2026-09-03", quality="EXACT"), _claim(["a1"])]))  # fmt: skip
    assert 0.8 < sources.score < 1.0 and "evidence sources missed: transactions" in sources.feedback
    assert PASS2.metric((), SimpleNamespace(claims=[])).score == 1.0


# --- C04 -------------------------------------------------------------------------------------


def test_c04_template_and_the_frozen_sections(tmp_path):
    from cfr_v2.routing.router import ClaimInput, claim_inputs

    ctx = FakeContext()
    path = tmp_path / "c04.xlsx"
    assert ROUTING.template(["GC-1"], path, ctx) == []
    sheet = load_workbook(path)["claims"]
    assert sheet["E2"].value == "S1: Procedure 1.pdf / 1.1 Section\nS2: Procedure 2.pdf / 2.4 Section"
    assert sheet["F2"].value == "UNMAPPED" and sheet["G2"].protection.locked is False
    _fill(path, "claims", {"1": [("correct", CATALOG.ids[0])], "2": [("correct", "NO_ELIGIBLE_VERTICAL")]})
    gold = ROUTING.read([path], ctx)
    assert gold.problems == [] and len(gold.examples) == 2
    first = gold.examples[0]
    assert first.inputs["section_ids"] == ["S1", "S2"] and "SECTION-TEXT 2.4" in first.inputs["sections"]
    claim = next(c for c in ClaimInput.from_claim_set(CLAIM_SET) if c.claim_id == "c1")
    assert first.inputs["claim"] == claim_inputs(claim, [])["claim"]
    assert first.label.correct == CATALOG.ids[0] and not gold.examples[1].inputs["section_ids"]
    _fill(path, "claims", {"1": [("correct", "nowhere")]})
    assert any("isn't a current vertical" in p for p in ROUTING.read([path], ctx).problems)


def test_c04_metric():
    from cfr_v2.optimize.spec_routing import Label

    routed = SimpleNamespace(routing=SimpleNamespace(outcome="ROUTED", vertical="fees"))
    assert ROUTING.metric(Label("fees"), routed).score == 1.0
    wrong = ROUTING.metric(Label("NO_ELIGIBLE_VERTICAL"), routed)
    assert wrong.score == 0.0 and wrong.feedback == "Answered fees; the right answer is NO_ELIGIBLE_VERTICAL."


# --- Pass 1 ----------------------------------------------------------------------------------

TRANSCRIPT = "Customer: I was charged a late fee after paying on time. Please refund the fee."


def test_pass1_reads_the_gold_workbook_and_scores_by_quote(tmp_path, monkeypatch):
    from cfr_v2.understanding import score as score_module
    from cfr_v2.understanding.pass1 import ExtractedAllegation, ExtractedRequest
    from cfr_v2.understanding.scoring import CaseText

    monkeypatch.setattr(score_module, "case_text", lambda store, ref: CaseText((("i1", TRANSCRIPT),)))
    path = tmp_path / "gold.xlsx"
    PASS1.template(["GC-1"], path, FakeContext())
    _fill(
        path,
        "cases",
        {
            "1": [
                ("labeller", "AB"),
                ("allegation_count", 1),
                ("request_count", 1),
                ("statement_count", 0),
                ("speaker_labels", "reliable"),
                ("could_not_label", "N"),
            ]
        },
    )
    _fill(
        path,
        "allegations",
        {
            "1": [
                ("case_no", "GC-1"),
                ("allegation_no", 1),
                ("allegation", "Late fee"),
                ("type", "Claim"),
                ("about", "system"),
                ("quote", "charged a late fee"),
                ("ambiguous", "N"),
            ]
        },
    )
    _fill(path, "requests", {"1": [("case_no", "GC-1"), ("request_no", 1), ("request", "Refund"),
                                   ("request_type", "refund_or_credit"), ("quote", "refund the fee"),
                                   ("for_allegations", 1), ("ambiguous", "N")]})  # fmt: skip
    gold = PASS1.read([path], FakeContext())
    assert gold.problems == [] and gold.examples[0].inputs == {"transcript": TRANSCRIPT}
    label = gold.examples[0].label

    def allegation(quote, kind="CLAIM"):
        return ExtractedAllegation(allegation="Customer says x", kind=kind, about="system", quotes=[quote],
                                   details=[], issue_indicators=[], ambiguous=False, ambiguity_note=None)  # fmt: skip

    request = ExtractedRequest(request="Customer asks x", type="refund_or_credit", quotes=["Please refund the fee"],
                               for_allegations=[1], ambiguous=False, ambiguity_note=None)  # fmt: skip
    right = SimpleNamespace(allegations=[allegation("charged a late fee after")], requests=[request],
                            agent_statements=[])  # fmt: skip
    assert PASS1.metric(label, right).score == 1.0
    missed = PASS1.metric(label, SimpleNamespace(allegations=[], requests=[request], agent_statements=[]))
    assert missed.score == pytest.approx(2 / 3) and "allegations: found 0 of 1" in missed.feedback
    invented = SimpleNamespace(allegations=[allegation("charged a late fee", "IMPACT"), allegation("on time")],
                               requests=[request], agent_statements=[])  # fmt: skip
    scored = PASS1.metric(label, invented)
    assert (
        scored.score < 1.0 and "wrong type on 1 of 1" in scored.feedback and "1 not in the gold set" in scored.feedback
    )
    assert "late fee" not in scored.feedback  # counts and types only


# --- a real run over a spec ---------------------------------------------------------------------


def test_a_run_over_the_c03_spec_end_to_end(tmp_path, monkeypatch):
    """The C03 program and metric work under an optimizer: per-call signatures, the JSON adapter, the cap."""
    from tests.unit.optimize.test_optimize import HINT
    from tests.unit.optimize.test_optimize import _fill as fill_schema

    ctx = FakeContext()
    cases = [f"GC-{i}" for i in range(24)]
    path = tmp_path / "c03.xlsx"
    SELECTION.template(cases, path, ctx)
    _fill(path, "cases", {str(i): [("primary_claim_no", 1)] for i in range(1, 25)})  # claim 1 is c2
    gold = SELECTION.read([path], ctx)
    assert len(gold.examples) == 24

    def responder(request):
        props = (request.output_schema or {}).get("properties", {})
        if "selection" in props:  # the selector: right only when told to look at what the customer asks for
            chosen = "c2" if "UPPERCASE" in request.instructions else "c1"
            return {
                "selection": {"status": "SELECTED", "claim_id": chosen, "candidate_claim_ids": [], "rationale": "x"}
            }
        if request.output_schema is None:
            return {"text": f"```\n{HINT}\n```"}
        return {k: fill_schema(v) for k, v in props.items()}

    report = session.run(SELECTION, BY_ID["c03.primary_selection"], gold, ctx,
                         client=FakeModelClient(responder=responder), model_id="fake", method="copro",
                         budget="light", cap=2000, seed=1, threads=2, out=tmp_path / "run", say=lambda line: None)  # fmt: skip
    assert report.stopped is None and report.better
    patch = (tmp_path / "run" / "patch.diff").read_text()
    assert '+SELECT_VERSION = "select-2"' in patch and "src/cfr_v2/selection/selector.py" in patch


def test_two_labellers_who_disagree_are_named(tmp_path):
    ctx = FakeContext()
    first, second = tmp_path / "ab.xlsx", tmp_path / "cd.xlsx"
    for path, answer in ((first, 1), (second, 2)):
        SELECTION.template(["GC-1"], path, ctx)
        _fill(path, "cases", {"1": [("primary_claim_no", answer)]})
    gold = SELECTION.read([first, second], ctx)
    assert gold.examples[0].label.primary == "c2"  # the first workbook's
    assert gold.problems == ["GC-1: labellers disagree; cd.xlsx's answer is not used"]
