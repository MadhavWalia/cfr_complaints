"""Routing (C04) review: today's workbook plus Sections useful?, scored per claim (LDR-0042 §5.5)."""

from __future__ import annotations

from cfr_v2.evaluation.routing import OUTCOMES, Routing, claim_rows, release_verticals
from tests.unit.evaluation.fakes import FakeReader, find, results, run

PASS2 = {
    "claims": [
        {"claim_id": "c1", "statement": "A late fee was charged although the payment was on time"},
        {"claim_id": "c2", "statement": "The agent promised a callback that never came"},
        {"claim_id": "c3", "statement": '=HYPERLINK("http://x")'},
    ]
}
SELECTED = {"status": "SELECTED", "claim_id": "c1", "rationale": "The fee is what they want put right."}
FEES = {"file_name": "Fees.pdf", "section_title": "Late fees", "page_start": 4, "page_end": 5}
ROUTING = {
    "claims": [
        {
            "claim_id": "c1",
            "outcome": "ROUTED",
            "rationale": "Claim-level reasoning.",
            "candidates": [
                {"vertical_id": "fees_and_interest", "rationale": "A disputed late fee.", "procedures": [FEES]}
            ],
        },
        {"claim_id": "c2", "outcome": "UNMAPPED", "rationale": "No vertical covers callbacks.", "candidates": []},
        {"claim_id": "c3", "outcome": "NO_ELIGIBLE_VERTICAL", "candidates": []},
    ]
}
STORED = {"c02.pass2": PASS2, "c03.primary_selection": SELECTED, "c04.routing": ROUTING}


def test_one_row_per_claim_with_its_primary_vertical_reasoning_and_sections():
    assert list(claim_rows("CASE-1", PASS2, SELECTED, ROUTING)) == [
        (
            "CASE-1",
            "A late fee was charged although the payment was on time",
            "Y",
            "fees_and_interest",
            "A disputed late fee.",
            "Fees.pdf · Late fees (p. 4–5)",
        ),
        (
            "CASE-1",
            "The agent promised a callback that never came",
            "N",
            "UNMAPPED",
            "No vertical covers callbacks.",
            "",
        ),
        ("CASE-1", '=HYPERLINK("http://x")', "N", "NO_ELIGIBLE_VERTICAL", "", ""),
    ]


def test_an_ambiguous_selection_has_no_primary_and_a_missing_statement_shows_the_claim_id():
    ambiguous = {"status": "AMBIGUOUS", "candidate_claim_ids": ["c1", "c2"]}
    assert [r[1:3] for r in claim_rows("CASE-2", None, ambiguous, ROUTING)] == [("c1", "N"), ("c2", "N"), ("c3", "N")]


def test_a_claim_routed_to_two_verticals_gets_a_row_each():
    one_page = dict(FEES, page_end=4)
    routing = {
        "claims": [
            {
                "claim_id": "c1",
                "outcome": "ROUTED",
                "rationale": "Shared reasoning.",
                "candidates": [
                    {"vertical_id": "fees_and_interest", "procedures": [one_page, FEES, FEES]},
                    {"vertical_id": "payments", "rationale": "Also a payment posting question."},
                ],
            }
        ]
    }
    assert [(r[3], r[4], r[5]) for r in claim_rows("CASE-3", PASS2, SELECTED, routing)] == [
        ("fees_and_interest", "Shared reasoning.", "Fees.pdf · Late fees (p. 4)\nFees.pdf · Late fees (p. 4–5)"),
        ("payments", "Also a payment posting question.", ""),
    ]


def test_the_workbook_keeps_todays_columns_and_adds_sections_useful(roundtrip):
    trip = roundtrip(Routing())
    book = trip.export(
        Routing().rows(run(), results(**STORED), FakeReader()),
        {"verticals": ("fees_and_interest", "payments", *OUTCOMES)},
    )
    sheet = book["routing"]
    assert [c.value for c in sheet[1]] == [
        "Case no", "Claim", "Primary (Y/N)", "Vertical", "Reasoning", "Sections",
        "Correct? (Y/N)", "Correct vertical (if not)", "Sections useful? (Y/Partly/N)", "Reason it's wrong",
    ]  # fmt: skip
    assert [c.value for c in book["_lists"]["B"]] == ["fees_and_interest", "payments", *OUTCOMES]


def test_without_the_release_verticals_the_dropdown_offers_those_in_the_rows(roundtrip, tmp_path):
    book = roundtrip(Routing()).export(Routing().rows(run(), results(**STORED), FakeReader()))
    lists = [[c.value for c in col] for col in book["_lists"].iter_cols()]
    assert ["fees_and_interest", *OUTCOMES] in lists
    assert release_verticals(tmp_path / "no-registry", "r", store=None) == []


def two_cases():
    one = Routing().rows(run("C-1", "run-1"), results(**STORED), FakeReader())
    other = dict(STORED, **{"c03.primary_selection": {"status": "SELECTED", "claim_id": "c2"}})
    return [*one, *Routing().rows(run("C-2", "run-2"), results(**other), FakeReader())]


def test_scored_per_claim_with_precision_recall_and_confusions(roundtrip):
    trip = roundtrip(Routing())
    trip.export(two_cases(), {"verticals": ("fees_and_interest", "payments", *OUTCOMES)})
    trip.answer("routing", {
        2: {"Correct? (Y/N)": "Y", "Sections useful? (Y/Partly/N)": "Y"},
        3: {"Correct? (Y/N)": "N", "Correct vertical (if not)": "servicing", "Reason it's wrong": "Callbacks are servicing", "Sections useful? (Y/Partly/N)": "Partly"},
        4: {"Correct? (Y/N)": "y"},
        5: {"Correct? (Y/N)": "N", "Correct vertical (if not)": "payments"},
        6: {"Correct? (Y/N)": "N", "Correct vertical (if not)": "servicing"},
    })  # fmt: skip
    report = trip.score()
    assert (report.headline.hits, report.headline.n) == (2, 5)  # claims, not cases
    assert report.counts["rows not reviewed"] == 1
    assert find(report, "Correct vertical")[:3] == [
        ["All claims", 0.4],
        ["Primary claims", 0.5],
        ["Other claims", 1 / 3],
    ]
    per = {row[0]: row[1:] for row in find(report, "Per vertical")}
    assert per["fees_and_interest"] == [2, 0.5, 1.0] and per["servicing"] == [0, None, 0.0]
    assert find(report, "Confusions") == [["UNMAPPED", "servicing", 2], ["fees_and_interest", "payments", 1]]
    assert find(report, "Sections useful?")[0] == ["All rows", 0.75]
    assert report.texts[0].rows == [
        ("C-1", "The agent promised a callback that never came", "UNMAPPED", "servicing", "Callbacks are servicing")
    ]
    assert "Reason" not in str(report.json())  # the reviewers' text stays out of the JSON


def test_a_claim_with_two_rows_is_right_only_if_both_are(roundtrip):
    routing = {
        "claims": [{"claim_id": "c1", "outcome": "ROUTED", "candidates": [{"vertical_id": "a"}, {"vertical_id": "b"}]}]
    }
    trip = roundtrip(Routing())
    trip.export(Routing().rows(run(), results(**{"c02.pass2": PASS2, "c04.routing": routing}), FakeReader()))
    trip.answer("routing", {2: {"Correct? (Y/N)": "Y"}, 3: {"Correct? (Y/N)": "N"}})
    report = trip.score()
    assert (report.headline.hits, report.headline.n) == (0, 1) and report.counts["claims with two verticals"] == 1


def test_a_wrong_row_whose_right_vertical_is_the_routed_one_is_left_out_and_named(roundtrip):
    trip = roundtrip(Routing())
    trip.export(Routing().rows(run(), results(**STORED), FakeReader()))
    trip.answer(
        "routing",
        {2: {"Correct? (Y/N)": "N", "Correct vertical (if not)": "fees_and_interest"}, 3: {"Correct? (Y/N)": "Y"}},
    )
    from cfr_v2.evaluation import workbook as wb

    review = wb.read(trip.path, Routing().sheets())
    report = Routing().score(review)
    assert report.headline.n == 1 and [p.line for p in review.problems] == [2]
    assert "the one it was routed to" in review.problems[0].reason
