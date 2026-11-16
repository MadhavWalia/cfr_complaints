"""Routing results to Excel (LDR-0033): one row per claim, as the case page reads the results, with
empty columns for a reviewer."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from cfr_v2.console import export
from cfr_v2.console.commands import BY_ID, Context

PASS2 = {
    "claims": [
        {"claim_id": "c1", "statement": "A late fee was charged although the payment was on time"},
        {"claim_id": "c2", "statement": "The agent promised a callback that never came"},
        {"claim_id": "c3", "statement": '=HYPERLINK("http://x")'},  # customer text can look like a formula
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


def test_one_row_per_claim_with_its_primary_vertical_reasoning_and_sections():
    rows = list(export.claim_rows("CASE-1", PASS2, SELECTED, ROUTING))
    assert rows == [
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
    rows = list(export.claim_rows("CASE-2", None, ambiguous, ROUTING))
    assert [r[1:3] for r in rows] == [("c1", "N"), ("c2", "N"), ("c3", "N")]


def test_a_claim_routed_to_two_verticals_gets_a_row_each_and_one_page_shows_once():
    one_page = dict(FEES, page_end=4)
    routing = {
        "claims": [
            {
                "claim_id": "c1",
                "outcome": "ROUTED",
                "rationale": "Shared reasoning.",
                "candidates": [
                    {"vertical_id": "fees_and_interest", "procedures": [one_page, FEES]},
                    {"vertical_id": "payments", "rationale": "Also a payment posting question."},
                ],
            }
        ]
    }
    rows = list(export.claim_rows("CASE-3", PASS2, SELECTED, routing))
    assert [(r[3], r[4], r[5]) for r in rows] == [
        ("fees_and_interest", "Shared reasoning.", "Fees.pdf · Late fees (p. 4)\nFees.pdf · Late fees (p. 4–5)"),
        ("payments", "Also a payment posting question.", ""),
    ]


def _write(path, verticals=()):
    report = export.Export()

    def rows():
        for row in export.claim_rows("CASE-1", PASS2, SELECTED, ROUTING):
            report.rows += 1
            yield row

    export.write_workbook(path, rows(), lambda: [("Rows", str(report.rows))], verticals)
    return load_workbook(path)


def _dropdowns(sheet):
    return {str(d.sqref): d.formula1 for d in sheet.data_validations.dataValidation}


def test_the_workbook_has_the_results_then_empty_review_columns_with_dropdowns(tmp_path):
    book = _write(tmp_path / "routing_results.xlsx", verticals=["fees_and_interest", "payments", "servicing"])
    sheet = book["routing"]
    assert [c.value for c in sheet[1]] == [
        "Case no",
        "Claim",
        "Primary (Y/N)",
        "Vertical",
        "Reasoning",
        "Sections",
        "Correct? (Y/N)",
        "Correct vertical (if not)",
        "Reason it's wrong",
    ]
    assert all(cell.value is None for row in sheet.iter_rows(min_row=2, min_col=7, max_col=9) for cell in row)
    assert sheet["G1"].fill.fgColor.rgb.endswith(export.REVIEW_FILL) and not sheet["A1"].fill.fgColor.rgb.endswith(
        export.REVIEW_FILL
    )
    assert _dropdowns(sheet) == {"G2:G4": '"Y,N"', "H2:H4": "=_lists!$A$1:$A$5"}
    lists = book["_lists"]
    assert lists.sheet_state == "hidden"
    assert [c.value for c in lists["A"]] == ["fees_and_interest", "payments", "servicing", *export.OUTCOMES]
    assert sheet["I2"].alignment.wrap_text  # room to write a reason


def test_without_the_release_verticals_the_dropdown_offers_those_in_the_rows(tmp_path):
    book = _write(tmp_path / "routing_results.xlsx")
    assert [c.value for c in book["_lists"]["A"]] == ["fees_and_interest", *export.OUTCOMES]


def test_text_stays_text_and_the_about_sheet_counts_after_streaming(tmp_path):
    book = _write(tmp_path / "routing_results.xlsx")
    sheet = book["routing"]
    formula_like = sheet["B4"]
    assert formula_like.value == '=HYPERLINK("http://x")' and formula_like.data_type == "s"  # never a formula
    assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref == "A1:I1"
    assert sheet["F2"].alignment.wrap_text and sheet["B2"].alignment.wrap_text
    assert [[c.value for c in r] for r in book["about"].iter_rows()] == [["Rows", "3"]]


def test_unreadable_runs_and_runs_without_a_routing_result_are_skipped_and_reported(monkeypatch):
    results = {
        "run-1": {"c02.pass2": PASS2, "c03.primary_selection": SELECTED, "c04.routing": ROUTING},
        "run-2": {"c02.pass2": PASS2},  # no C04 result
    }

    def read(db, store, run_id):
        if run_id == "run-3":
            raise OSError("store unavailable")
        return results[run_id]

    monkeypatch.setattr(export, "read_results", read)
    report = export.Export()
    rows = list(export.rows_for(None, None, [("run-1", "A"), ("run-2", "B"), ("run-3", "C")], report))
    assert len(rows) == 3 and {r[0] for r in rows} == {"A"}
    assert (report.runs, report.rows, report.skipped) == (1, 3, ["run-2", "run-3"])


def test_the_release_verticals_fall_back_to_none_when_they_cant_be_read(tmp_path):
    assert export.release_verticals(tmp_path / "no-registry", "cfr-v2-phase-e", store=None) == []


def test_only_plan_ready_runs_of_the_release_in_the_window():
    sql = export.RUNS_SQL
    assert "latest.state = 'PLAN_READY'" in sql and "rm.release_id = %(release)s" in sql
    assert "r.created_at >= %(start)s" in sql and "r.created_at < %(end)s" in sql


def test_the_cli_refuses_a_window_that_ends_before_it_starts(tmp_path):
    out = str(tmp_path / "x.xlsx")
    with pytest.raises(SystemExit) as exit_:
        export.main(["--release", "r", "--admitted-from", "2026-11-16", "--admitted-to", "2026-11-15", "--out", out])
    assert exit_.value.code == 2


def test_the_console_form_defaults_to_the_current_release_and_writes_into_the_job(tmp_path):
    folder = tmp_path / "registry" / "releases"
    folder.mkdir(parents=True)
    (folder / "cfr-v2-phase-e.yaml").write_text("release_id: x\n", encoding="utf-8")
    command = BY_ID["evaluation.routing_export"]
    ctx = Context(root=tmp_path, env={"CFR_RELEASE_ID": "cfr-v2-phase-e"})
    assert command.tags == ("changes nothing", "keep output local")
    values = command.parse({}, ctx)
    assert command.argv(values, Path("/job"))[1:] == [
        "-m",
        "cfr_v2.console.export",
        "--release",
        "cfr-v2-phase-e",
        "--out",
        "/job/outputs/routing_results.xlsx",
    ]
    window = {"release": "cfr-v2-phase-e", "admitted_from": "2026-11-15", "admitted_to": "2026-11-16"}
    assert command.argv(command.parse(window, ctx), Path("/job"))[5:9] == [
        "--admitted-from",
        "2026-11-15",
        "--admitted-to",
        "2026-11-16",
    ]


def test_control_characters_from_a_call_are_dropped_not_fatal(tmp_path):
    """A claim with a vertical tab in it stopped a real export (2026-11-16): openpyxl refuses
    characters Excel can't store. They print nothing, so dropping them keeps the text as read."""
    routing = {
        "claims": [
            {
                "claim_id": "c1",
                "outcome": "UNMAPPED",
                "rationale": "The first agent said there were no 10\x0ba2 charges.\x01",
                "candidates": [],
            }
        ]
    }
    pass2 = {"claims": [{"claim_id": "c1", "statement": "Told the emails\x1b were fraudulent\nand\tso on"}]}
    rows = export.claim_rows("CASE-9\x00", pass2, None, routing)
    export.write_workbook(tmp_path / "x.xlsx", rows, lambda: [("Note", "bell\x07")])
    book = load_workbook(tmp_path / "x.xlsx")
    row = [c.value for c in book["routing"][2]]
    assert row[:5] == [
        "CASE-9",
        "Told the emails were fraudulent\nand\tso on",  # newline and tab are allowed, and kept
        "N",
        "UNMAPPED",
        "The first agent said there were no 10a2 charges.",
    ]
    assert book["about"]["B1"].value == "bell"
