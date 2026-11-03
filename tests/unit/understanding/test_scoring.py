"""Gold-set scoring: workbook rules, quote location across redaction, matching and metrics."""

from __future__ import annotations

import json

import pytest
from openpyxl import load_workbook

from cfr_v2.contracts import (
    AgentStatement,
    AgentStatementKind,
    Allegation,
    AllegationDetail,
    ClaimScope,
    CustomerRequest,
    DetailType,
    Pass1Result,
    RedactionMap,
    RedactionSpan,
    RequestType,
    SpanRef,
    StatementKind,
)
from cfr_v2.understanding.gold import GoldError, read_gold, write_template
from cfr_v2.understanding.scoring import CaseText, Report, gold_side, overlap, pipeline_side, score_sides

H = "a" * 64
ORIGINAL = (
    "Agent: How can I help? Customer: This is John Smith, I was charged twice for my hotel in Lisbon. "
    "Please refund the second charge. Agent: I have opened a dispute for the second charge."
)
REDACTED = ORIGINAL.replace("John Smith", "⟦PII⟧ ⟦PII⟧")
NAME_AT = ORIGINAL.index("John Smith")
MAP = RedactionMap(
    interaction_id="i1",
    original_sha256=H,
    redacted_sha256=H,
    redaction_version="v",
    spans=(
        RedactionSpan(
            original_start=NAME_AT, original_end=NAME_AT + 4, redacted_start=NAME_AT, redacted_end=NAME_AT + 5
        ),
        RedactionSpan(
            original_start=NAME_AT + 5,
            original_end=NAME_AT + 10,
            redacted_start=NAME_AT + 6,
            redacted_end=NAME_AT + 11,
        ),
    ),
)
TEXT = CaseText((("i1", REDACTED),), (("i1", ORIGINAL, MAP),))


def _span(quote: str) -> SpanRef:
    return TEXT.locate(quote)


# --- redaction mapping and quote location ------------------------------------------------


def test_to_redacted_is_the_inverse_of_to_original():
    start = ORIGINAL.index("This is")
    end = ORIGINAL.index("charged") + len("charged")
    r_start, r_end = MAP.to_redacted(start, end)
    assert REDACTED[r_start:r_end] == "This is ⟦PII⟧ ⟦PII⟧, I was charged"
    assert MAP.to_original(r_start, r_end) == (start, end)
    # A boundary inside a name covers the whole token that replaced it.
    assert REDACTED[slice(*MAP.to_redacted(NAME_AT + 2, NAME_AT + 7))] == "⟦PII⟧ ⟦PII⟧"


def test_gold_quote_with_a_name_is_located_through_the_original():
    span = _span("This is John Smith, I was charged twice")
    assert REDACTED[span.start : span.end] == "This is ⟦PII⟧ ⟦PII⟧, I was charged twice"
    assert _span("charged twice for my hotel").interaction_id == "i1"
    assert _span("never said") is None


# --- workbook -----------------------------------------------------------------------------


def _workbook(tmp_path, rows: dict[str, list[list]], cases=("GC-1",)):
    path = tmp_path / "gold_AB_2026-11-10.xlsx"
    write_template(list(cases), path)
    book = load_workbook(path)
    for i, case in enumerate(cases, start=2):
        book["cases"].cell(row=i, column=2, value="AB")
        book["cases"].cell(row=i, column=6, value="reliable")
        book["cases"].cell(row=i, column=7, value="N")
    for sheet, values in rows.items():
        for row in values:
            book[sheet].append(row)
    book.save(path)
    return path


GOLD_ROWS = {
    "allegations": [
        [
            "GC-1",
            1,
            "Customer says the hotel charged twice",
            "Claim",
            "third_party",
            "This is John Smith, I was charged twice for my hotel",
            None,
            "N",
            None,
        ],
    ],
    "details": [["GC-1", 1, "merchant_or_product", "hotel in Lisbon", "my hotel in Lisbon"]],
    "requests": [
        ["GC-1", 1, "Customer asks for a refund", "refund_or_credit", "Please refund the second charge", "1", "N", None]
    ],
    "agent_statements": [
        [
            "GC-1",
            1,
            "Agent says a dispute was opened",
            "Action",
            "I have opened a dispute for the second charge",
            None,
            "1",
            "1",
            "N",
            None,
        ],
    ],
}


def test_template_round_trips_through_the_reader(tmp_path):
    [case] = read_gold(_workbook(tmp_path, GOLD_ROWS))
    assert (case.case_no, case.labeller, case.speaker_labels, case.could_not_label) == ("GC-1", "AB", "reliable", False)
    assert case.allegations[0].kind == "CLAIM" and case.allegations[0].about == "third_party"
    assert case.requests[0].allegations == {1}
    assert case.statements[0].kind == "ACTION" and case.statements[0].requests == {1}
    book = load_workbook(tmp_path / "gold_AB_2026-11-10.xlsx")
    assert book["allegations"].data_validations.dataValidation  # dropdowns in the template


@pytest.mark.parametrize(
    ("sheet", "row", "error"),
    [
        ("allegations", ["GC-1", 1, "x", "Complaint", "system", "q", None, "N", None], "allegations row 2: type"),
        ("allegations", ["GC-9", 1, "x", "Claim", "system", "q", None, "N", None], "not on the cases sheet"),
        ("requests", ["GC-1", 1, "x", "refund_or_credit", "q", "7", "N", None], "links to an allegation"),
        ("details", ["GC-1", 4, "when", "x", "q"], "not an allegation of this case"),
        ("requests", ["GC-1", 1, "x", "refund_or_credit", "q", "one", "N", None], "numbers separated by commas"),
    ],
)
def test_workbook_problems_name_the_sheet_and_row_not_the_text(tmp_path, sheet, row, error):
    rows = {"allegations": [["GC-1", 1, "SECRET TEXT", "Claim", "system", "q", None, "N", None]]}
    rows.setdefault(sheet, [])
    rows[sheet] = rows[sheet][:0] if sheet == "allegations" else rows[sheet]
    rows[sheet].append(row)
    with pytest.raises(GoldError, match=error) as caught:
        read_gold(_workbook(tmp_path, rows))
    assert "SECRET" not in str(caught.value)


# --- matching and metrics -----------------------------------------------------------------


def _pipeline(**overrides) -> Pass1Result:
    allegation = Allegation(
        allegation_id="a1",
        text="Customer says charged twice",
        kind=StatementKind.CLAIM,
        scope=ClaimScope.THIRD_PARTY,
        spans=(_span("I was charged twice for my hotel in Lisbon"),),
        details=(
            AllegationDetail(detail_type=DetailType.MERCHANT_OR_PRODUCT, text="hotel", span=_span("hotel in Lisbon")),
        ),
    )
    request = CustomerRequest(
        request_id="r1",
        text="Customer asks for a refund",
        request_type=RequestType.REFUND_OR_CREDIT,
        spans=(_span("refund the second charge"),),
        allegation_ids=("a1",),
    )
    statement = AgentStatement(
        statement_id="s1",
        text="Agent says dispute opened",
        kind=AgentStatementKind.ACTION,
        spans=(_span("opened a dispute"),),
        allegation_ids=("a1",),
        request_ids=("r1",),
    )
    fields = {"allegations": (allegation,), "requests": (request,), "agent_statements": (statement,)}
    return Pass1Result(
        case_id="GC-1",
        program_id="c02.pass1",
        program_version="pass1-2",
        transcript_ref="sha256:" + "1" * 64,
        **(fields | overrides),
    )


def _gold(tmp_path):
    [case] = read_gold(_workbook(tmp_path, GOLD_ROWS))
    side, problems = gold_side(case, TEXT)
    assert problems == []
    return side


def test_agreeing_pipeline_scores_full_marks(tmp_path):
    scores = score_sides(_gold(tmp_path), pipeline_side(_pipeline()), min_overlap=0.3)
    for name in ("allegations", "details", "requests", "statements"):
        s = scores[name]
        assert (s.gold, s.predicted, s.matched, s.recall, s.precision) == (1, 1, 1, 1.0, 1.0), name
    assert scores["allegations"].kind_agreement == 1.0 and scores["allegations"].about_agreement == 1.0
    assert scores["requests"].link_accuracy == 1.0 and scores["statements"].link_accuracy == 1.0


def test_misses_extras_disagreements_and_wrong_links_are_counted(tmp_path):
    base = _pipeline()
    extra = base.agent_statements[0].model_copy(
        update={
            "statement_id": "s2",
            "spans": (_span("How can I help?"),),
            "kind": AgentStatementKind.FACT,
            "request_ids": (),
        }
    )
    wrong = base.agent_statements[0].model_copy(update={"kind": AgentStatementKind.FACT, "request_ids": ()})
    result = _pipeline(requests=(), agent_statements=(wrong, extra))
    scores = score_sides(_gold(tmp_path), pipeline_side(result), min_overlap=0.3)
    assert scores["requests"].recall == 0.0 and scores["requests"].predicted == 0
    st = scores["statements"]
    assert (st.gold, st.predicted, st.matched, st.precision) == (1, 2, 1, 0.5)
    assert st.kind_agreement == 0.0 and st.link_accuracy == 0.0


def test_overlap_threshold_decides_a_match():
    long = (_span("I was charged twice for my hotel in Lisbon. Please refund the second charge"),)
    short = (_span("Lisbon"),)
    assert overlap(long, short) == 1.0  # all of the shorter side is shared
    assert overlap((_span("How can I help?"),), short) == 0.0


def test_report_carries_numbers_and_ids_only(tmp_path):
    report = Report()
    result = _pipeline()
    report.add_case("GC-1", score_sides(_gold(tmp_path), pipeline_side(result), 0.3), "Billing", result)
    dumped = json.dumps(report.as_dict())
    for text in ("John", "hotel", "refund", "Lisbon", "dispute"):
        assert text not in dumped
    assert report.as_dict()["totals"]["allegations"]["recall"] == 1.0
    assert report.dropped_rate == 0.0
