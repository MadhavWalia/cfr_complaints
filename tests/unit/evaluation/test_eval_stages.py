"""Selection, Pass 2, Pass 1 and Intake reviews: what each workbook holds and how it scores (LDR-0042 §5)."""

from __future__ import annotations

from cfr_v2.evaluation.intake import Intake
from cfr_v2.evaluation.pass1 import Pass1
from cfr_v2.evaluation.pass2 import Pass2
from cfr_v2.evaluation.selection import Selection
from cfr_v2.evaluation.source import Transcript
from tests.unit.evaluation.fakes import FakeReader, find, results, run

ALLEGATIONS = [
    {"allegation_id": "a1", "text": "The fee was wrong", "kind": "CLAIM", "scope": "system", "spans": [{"interaction_id": "i1", "start": 0, "end": 9}]},
    {"allegation_id": "a2", "text": "The agent was rude", "kind": "CLAIM", "scope": "agent", "spans": [{"interaction_id": "i1", "start": 10, "end": 19}]},
    {"allegation_id": "a3", "text": "Card was blocked", "kind": "CLAIM", "scope": "system", "spans": [{"interaction_id": "i1", "start": 20, "end": 25}]},
]  # fmt: skip
CLAIMS = [  # stored out of order: the workbook numbers them in the order raised
    {"claim_id": "c2", "statement": "Rude agent", "allegation_ids": ["a2"], "account_resolution": "NOT_APPLICABLE", "time_quality": "UNKNOWN", "grounding_status": "PARTIAL"},
    {"claim_id": "c1", "statement": "Wrong late fee", "allegation_ids": ["a1"], "account_refs": ["A1"], "account_resolution": "RESOLVED",
     "event_window_start": "2026-10-01", "event_window_end": "2026-10-02", "time_quality": "EXACT", "grounding_status": "GROUNDED",
     "evidence_needs": [{"need_id": "n1", "source_type": "billing", "description": "Fee history"}]},
    {"claim_id": "c3", "statement": "Blocked card", "allegation_ids": ["a3"], "account_resolution": "NONE", "time_quality": "APPROXIMATE", "grounding_status": "PARTIAL"},
]  # fmt: skip
PASS2 = {
    "allegations": ALLEGATIONS,
    "claims": CLAIMS,
    "requests": [
        {"request_id": "r1", "text": "Refund the fee", "request_type": "refund_or_credit", "allegation_ids": ["a1"]}
    ],
    "other_checks": [
        {"check_id": "k1", "origin": "s1", "description": "Agent said refund in 5 days", "source_types": ["billing"]}
    ],
    "findings": [{"issue": "WINDOW_AFTER_RECEIVED", "ref": "c1"}, {"issue": "NOT_COVERED", "ref": "s2"}],
}


# --- Selection -------------------------------------------------------------------------------


def selection_case(case_no, run_id, answer):
    return list(
        Selection().rows(
            run(case_no, run_id), results(**{"c02.pass2": PASS2, "c03.primary_selection": answer}), FakeReader()
        )
    )


def test_selection_numbers_claims_in_the_order_raised_and_shows_the_answer(roundtrip):
    trip = roundtrip(Selection())
    trip.export(selection_case("C-1", "run-1", {"status": "SELECTED", "claim_id": "c1", "rationale": "The fee."}))
    row = trip.rows("cases")[0]
    assert row["Claims"] == "C1 Wrong late fee\nC2 Rude agent\nC3 Blocked card"
    assert row["Requests"] == "Refund the fee (about C1)"
    assert (row["Answer"], row["Primary"], row["Decided without the model"], row["Ref"]) == (
        "SELECTED",
        "C1 Wrong late fee",
        "N",
        "run-1",
    )


def test_selection_top1_weighted_and_ambiguous_agreement(roundtrip):
    trip = roundtrip(Selection())
    single = {"allegations": ALLEGATIONS[:1], "claims": CLAIMS[1:2]}
    trip.export([
        *selection_case("C-1", "run-1", {"status": "SELECTED", "claim_id": "c1", "rationale": "x"}),
        *selection_case("C-2", "run-2", {"status": "SELECTED", "claim_id": "c2", "rationale": "x"}),
        *selection_case("C-3", "run-3", {"status": "AMBIGUOUS", "candidate_claim_ids": ["c1", "c2"], "rationale": "x"}),
        *selection_case("C-4", "run-4", {"status": "AMBIGUOUS", "candidate_claim_ids": ["c1", "c3"], "rationale": "x"}),
        *Selection().rows(run("C-5", "run-5"), results(**{"c02.pass2": single, "c03.primary_selection": {"status": "SELECTED", "claim_id": "c1", "rationale": "x"}}), FakeReader()),
    ])  # fmt: skip
    trip.answer("cases", {
        2: {"Correct? (Y/N)": "Y"},
        3: {"Correct? (Y/N)": "N", "Right primary (if not)": "C1"},
        4: {"Correct? (Y/N)": "Y"},  # agrees it is ambiguous
        5: {"Correct? (Y/N)": "N", "Right primary (if not)": "C3"},
        6: {"Correct? (Y/N)": "Y"},
    })  # fmt: skip
    report = trip.score()
    assert (report.headline.hits, report.headline.n) == (1, 3)  # C-1 right, C-2 wrong, C-4 ambiguous on a clear case
    table = {r[0]: r[1] for r in find(report, "Primary claim")}
    assert table["Weighted score"] == (1 + 0 + 1 + 0.25) / 4
    assert table["AMBIGUOUS answers the reviewer agrees with"] == 0.5 and table["AMBIGUOUS rate"] == 0.5
    assert table["Cases decided without the model, right"] == 1.0 and report.counts["decided without the model"] == 1


# --- Pass 2 ----------------------------------------------------------------------------------

BASELINE = {"accounts": [{"account_ref": "A1", "product": "Platinum Card", "is_case_account": True}]}


def test_pass2_rows_per_claim_with_account_names_corrections_and_a_cases_sheet(roundtrip):
    trip = roundtrip(Pass2())
    book = trip.export(
        Pass2().rows(run(), results(**{"c02.pass2": PASS2}), FakeReader(baseline=BASELINE)),
        {"sources": ("billing", "calls")},
    )
    first = trip.rows("claims")[0]
    assert (first["Claim no"], first["Claim"], first["Allegations"]) == ("C1", "Wrong late fee", "a1 The fee was wrong")
    assert (first["Account"], first["Accounts"], first["Event window"], first["Time quality"]) == (
        "Resolved",
        "A1 · Platinum Card",
        "2026-10-01 – 2026-10-02",
        "Exact",
    )
    assert first["Evidence needs"] == "billing: Fee history"
    assert first["Corrections"] == "The event window was cut back to the received date"
    case = trip.rows("cases")[0]
    assert case["Claims"] == 3 and case["Other checks"] == "s1 Agent said refund in 5 days [billing]"
    assert case["Case corrections"] == "An agent statement or request had no need or check"
    assert ["billing", "calls"] in [[c.value for c in col if c.value] for col in book["_lists"].iter_cols()]


def test_pass2_precision_estimated_recall_and_per_field_accuracy(roundtrip):
    trip = roundtrip(Pass2())
    trip.export(Pass2().rows(run(), results(**{"c02.pass2": PASS2}), FakeReader(baseline=BASELINE)))
    trip.answer("claims", {
        2: {"Claim correct? (Y/Partly/N)": "Y", "Grouping": "Right", "Account right?": "Y", "Event window right?": "N", "Evidence needs right? (Y/Partly/N)": "Partly", "Missing evidence source": "calls"},
        3: {"Claim correct? (Y/Partly/N)": "Partly", "Grouping": "Should be split", "Account right?": "Not about an account"},
        4: {"Claim correct? (Y/Partly/N)": "N"},
    })  # fmt: skip
    trip.answer("cases", {2: {"Missed claims": 1, "Other checks right?": "Y"}})
    report = trip.score()
    assert report.headline.value == 0.5  # (1 + 0.5 + 0) / 3
    main = {r[0]: r[1] for r in find(report, "Claims")}
    assert main["Claim precision, strict"] == 1 / 3 and main["Estimated claim recall"] == 1.5 / 2.5
    assert main["Grouping right"] == 0.5 and main["Should be split"] == 1
    assert find(report, "Account right?, by resolution")[0] == ["Resolved", 1, 1.0]
    assert find(report, "Most missed evidence sources") == [["calls", 1, 1.0]]


# --- Pass 1 ----------------------------------------------------------------------------------

TEXT = "Fee wrong Rude agt Block"
PASS1 = {
    "transcript_ref": "sha256:t",
    "allegations": [dict(ALLEGATIONS[0], details=[{"detail_type": "amount", "text": "$39", "span": {"interaction_id": "i1", "start": 4, "end": 9}}]), ALLEGATIONS[1]],
    "requests": [{"request_id": "r1", "text": "Refund", "request_type": "refund_or_credit", "spans": [{"interaction_id": "i1", "start": 0, "end": 3}], "allegation_ids": ["a1"]}],
    "agent_statements": [{"statement_id": "s1", "text": "Refund in 5 days", "kind": "COMMITMENT", "spans": [{"interaction_id": "i1", "start": 20, "end": 25}], "ambiguous": True, "ambiguity_note": "unclear when"}],
    "dropped": [{"item_list": "allegations", "index": 3, "reason": "QUOTE_NOT_FOUND"}],
}  # fmt: skip
TRANSCRIPT = Transcript({"i1": TEXT}, "Y", 2, len(TEXT))


def test_pass1_rows_hold_every_quote_from_the_redacted_transcript(roundtrip):
    trip = roundtrip(Pass1())
    trip.export(Pass1().rows(run(), results(**{"c02.pass1": PASS1}), FakeReader(TRANSCRIPT)))
    items = trip.rows("items")
    assert [(i["List"], i["No"], i["Type"]) for i in items] == [
        ("Allegation", "a1", "CLAIM"), ("Allegation", "a2", "CLAIM"), ("Request", "r1", "refund_or_credit"), ("Agent statement", "s1", "COMMITMENT"),
    ]  # fmt: skip
    assert items[0]["Quotes"] == "Fee wrong\nwrong" and items[0]["Details"] == "amount: $39"  # every span, in order
    assert items[3]["Ambiguous"] == "Y: unclear when" and items[2]["Linked to"] == "a1"
    assert trip.rows("cases")[0] == {
        "Case no": "C-1",
        "Allegations": 2,
        "Requests": 1,
        "Agent statements": 1,
        "Dropped": 1,
        "Missed allegations": None,
        "Missed requests": None,
        "Missed agent statements": None,
        "What was missed": None,
        "Notes": None,
        "Ref": "run-1",
    }


def test_pass1_precision_type_accuracy_and_estimated_recall_per_list(roundtrip):
    trip = roundtrip(Pass1())
    trip.export(Pass1().rows(run(), results(**{"c02.pass1": PASS1}), FakeReader(TRANSCRIPT)))
    trip.answer("items", {
        2: {"Correct? (Y/Partly/N)": "Y", "Quotes support it? (Y/Partly/N)": "Y"},
        3: {"Correct? (Y/Partly/N)": "N", "Should not be here because": "Not said"},
        4: {"Correct? (Y/Partly/N)": "Partly", "Right type (if not)": "fee_reversal"},
        5: {"Correct? (Y/Partly/N)": "Y"},
    })  # fmt: skip
    trip.answer(
        "cases",
        {
            2: {
                "Missed allegations": 1,
                "Missed requests": 0,
                "Missed agent statements": 0,
                "What was missed": "The card block",
            }
        },
    )
    report = trip.score()
    assert report.headline.value == 0.5
    lists = {r[0]: r[1:] for r in find(report, "Per list")}
    assert lists["Allegation"][:4] == [2, 0.5, 0.5, 0.5]  # recall 1 / (1 + 1)
    assert lists["Request"][1] == 0.5 and lists["All items"][0] == 4
    types = {r[0]: r[1] for r in find(report, "Types and quotes")}
    assert types["Request"] == 0.0 and types["Allegation"] == 1.0
    assert find(report, "Why items shouldn't be there") == [["Not said", 1, 1.0]]
    assert report.texts[0].rows == [("C-1", "The card block")]


# --- Intake ----------------------------------------------------------------------------------


def test_intake_rows_show_the_transcript_start_and_how_it_was_linked(roundtrip):
    long = Transcript({"i1": "x" * 2000}, "N", 3, 2000)
    trip = roundtrip(Intake())
    trip.export([
        *Intake().rows(run("C-1", "run-1"), results(**{"c02.pass1": PASS1}), FakeReader(long)),
        *Intake().rows(run("C-2", "run-2"), results(**{"c02.pass1": {"transcript_ref": None}}), FakeReader(None)),
    ])  # fmt: skip
    first, second = trip.rows("cases")
    assert (first["Admitted"], first["Calls joined"], first["Characters"], first["Agent matched"]) == (
        "2026-11-20",
        3,
        2000,
        "N",
    )
    assert len(first["Transcript start"]) == 1501 and first["Transcript start"].endswith("…")
    assert second["Transcript start"] == "(no transcript)"


def test_intake_right_call_rate_leaves_cant_tell_and_no_transcript_out(roundtrip):
    trip = roundtrip(Intake())
    stored = results(**{"c02.pass1": PASS1})
    trip.export(
        [row for i in range(4) for row in Intake().rows(run(f"C-{i}", f"run-{i}"), stored, FakeReader(TRANSCRIPT))]
    )
    trip.answer("cases", {
        2: {"Right call?": "Y", "Extra calls?": "N"},
        3: {"Right call?": "Y", "Extra calls?": "Y", "Problem": "Several calls, one right"},
        4: {"Right call?": "N", "Problem": "Wrong customer"},
        5: {"Right call?": "can't tell"},
    })  # fmt: skip
    report = trip.score()
    assert (report.headline.hits, report.headline.n) == (2, 3)
    main = {r[0]: r[1] for r in find(report, "Transcripts")}
    assert main["Right call and nothing else mixed in"] == 0.5 and main["Can't tell"] == 0.25
    assert find(report, "By agent match") == [["Y", 3, 2 / 3]]
