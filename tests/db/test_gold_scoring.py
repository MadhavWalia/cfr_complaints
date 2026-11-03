"""Gold-set scoring end to end: a committed Pass 1 result scored against two labellers' workbooks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from cfr_v2.intake import IntakeCoordinator, ProfanityFilter
from cfr_v2.intake.fakes import RegexMasker, synthetic_names, synthetic_rows
from cfr_v2.llm import FakeModelClient
from cfr_v2.llm.dspy_lm import gateway_lm
from cfr_v2.registry import DEFAULT_REGISTRY_DIR, ReleaseRegistry
from cfr_v2.runtime import FINAL_STATE
from cfr_v2.understanding import make_pass1_handler
from cfr_v2.understanding.gold import write_template
from cfr_v2.understanding.score import score

from .spike.conftest import wait_for_state

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[2]
NAME = synthetic_names()[0]
TRANSCRIPT = (
    f"Agent: How can I help? Customer: This is {NAME}, I was charged twice for my hotel in Lisbon. "
    "Please refund the second charge. Agent: I have opened a dispute for the second charge."
)


def _respond(request):
    item = {"ambiguous": False, "ambiguity_note": None}
    return {
        "allegations": [
            item
            | {
                "allegation": "Customer says they were charged twice",
                "kind": "CLAIM",
                "about": "third_party",
                "quotes": ["I was charged twice for my hotel in Lisbon"],
                "details": [{"type": "merchant_or_product", "detail": "hotel", "quote": "hotel in Lisbon"}],
                "issue_indicators": [],
            }
        ],
        "requests": [
            item
            | {
                "request": "Customer asks for a refund",
                "type": "refund_or_credit",
                "quotes": ["Please refund the second charge"],
                "for_allegations": [1],
            }
        ],
        "agent_statements": [
            item
            | {
                "statement": "Agent says a dispute was opened",
                "kind": "ACTION",
                "quotes": ["I have opened a dispute for the second charge"],
                "responds_to_allegations": [1],
                "responds_to_requests": [1],
            }
        ],
    }


def _gold(path: Path, case_no: str, labeller: str, statement_type: str) -> Path:
    write_template([case_no], path)
    book = load_workbook(path)
    book["cases"]["B2"], book["cases"]["F2"], book["cases"]["G2"] = labeller, "reliable", "N"
    book["allegations"].append(
        [
            case_no,
            1,
            "Customer says the hotel charged twice",
            "Claim",
            "third_party",
            f"This is {NAME}, I was charged twice for my hotel",
            None,
            "N",
            None,
        ]  # a quote with a redacted name
    )
    book["details"].append([case_no, 1, "merchant_or_product", "hotel in Lisbon", "my hotel in Lisbon"])
    book["requests"].append(
        [case_no, 1, "Customer asks for a refund", "refund_or_credit", "refund the second charge", "1", "N", None]
    )
    book["agent_statements"].append(
        [
            case_no,
            1,
            "Agent says a dispute was opened",
            statement_type,
            "opened a dispute for the second charge",
            None,
            "1",
            "1",
            "N",
            None,
        ]
    )
    book["agent_statements"].append(
        [case_no, 2, "Agent offers help", "Advice", "How can I help?", None, None, None, "Y", "Greeting?"]
    )
    book.save(path)
    return path


def test_score_against_two_labellers(running, store, tmp_path):
    running.handlers = {
        **running.handlers,
        "c02.pass1": make_pass1_handler(store, gateway_lm(FakeModelClient(responder=_respond), "fake-model")),
    }
    row = synthetic_rows(1, seed=21)[0] | {"msg_tx2": TRANSCRIPT}
    manifest = ReleaseRegistry(REPO_ROOT / DEFAULT_REGISTRY_DIR).load("cfr-v2-phase-c")
    coordinator = IntakeCoordinator.pinned(
        manifest, store=store, masker=RegexMasker(synthetic_names()), profanity=ProfanityFilter()
    )
    [admitted] = coordinator.admit_rows([row]).results
    wait_for_state(running.settings.state_database_url, [admitted.case_run_id], FINAL_STATE, timeout=60)
    case_no = row["case_no"]

    first = _gold(tmp_path / "gold_AB.xlsx", case_no, "AB", "Action")
    second = _gold(tmp_path / "gold_CD.xlsx", case_no, "CD", "Fact")
    report = score([first, second], db=running.db, store=store)

    assert report.cases_scored == [case_no] and report.quote_problems == []
    totals = report.totals
    for name in ("allegations", "details", "requests"):
        assert (totals[name].recall, totals[name].precision) == (1.0, 1.0), name
    assert totals["allegations"].about_agreement == 1.0 and totals["requests"].link_accuracy == 1.0
    st = totals["statements"]
    assert (st.gold, st.matched, st.precision, st.recall) == (2, 1, 1.0, 0.5)  # the greeting row was missed
    assert st.unambiguous_recall == 1.0  # ...but it was marked ambiguous
    assert report.allegations_by_category  # one category bucket

    agreement = report.agreement["statements"]
    assert report.agreement_cases == [case_no]
    assert (agreement.matched, agreement.kind_agreement) == (2, 0.5)  # Action vs Fact on statement 1

    dumped = json.dumps(report.as_dict())
    assert NAME not in dumped and "Lisbon" not in dumped and "refund" not in dumped
