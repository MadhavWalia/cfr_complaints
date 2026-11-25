"""Rates and their ranges, the run selection, and ``python -m cfr_v2.evaluation`` (LDR-0042 §4, §6)."""

from __future__ import annotations

import json

import pytest
from openpyxl import load_workbook

from cfr_v2.evaluation import __main__ as cli
from cfr_v2.evaluation import source
from cfr_v2.evaluation.report import Rate, share
from cfr_v2.evaluation.routing import Routing
from tests.unit.evaluation.fakes import FakeReader, results, run
from tests.unit.evaluation.test_eval_routing import STORED


def test_a_rate_has_a_wilson_range_and_is_small_under_30():
    assert Rate(5, 10).interval == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert Rate(0, 10).interval == pytest.approx((0.0, 0.2775), abs=1e-4)  # sensible at 0%, unlike the normal interval
    assert Rate(10, 10).interval[1] == pytest.approx(1.0)
    assert Rate(5, 10).small and not Rate(15, 30).small
    assert Rate(0, 0).value is None and Rate(0, 0).json()["rate"] is None
    assert str(Rate(1, 4)) == "25.0% (n = 4, 4.6%–69.9%)"


def test_partly_counts_half_and_strict_counts_only_yes():
    answers = ["Y", "Partly", "N", None]
    assert (share(answers).hits, share(answers).n) == (1.5, 3)
    assert share(answers, partly=None).hits == 1


def test_runs_are_one_per_case_latest_under_the_release_with_the_stage_done():
    sql = source.LATEST_RUNS_SQL
    assert "DISTINCT ON (c.source_case_id)" in sql and "rm.release_id = %(release)s" in sql
    assert "t.logical_task_id = %(task)s AND t.status = 'SUCCEEDED'" in sql
    assert "md5(case_run_id || %(seed)s::text)" in sql and "LIMIT %(limit)s" in sql


def test_a_random_sample_always_gets_a_seed_so_it_can_be_repeated():
    choice = source.Choice(sample=50).with_seed()
    assert choice.mode == "sample" and len(choice.seed) == 8
    assert source.Choice(sample=50, seed="7").with_seed().seed == "7"
    assert source.Choice(case_nos=("A", "B")).describe() == "2 cases listed"


def test_case_lists_split_on_lines_commas_and_spaces_and_skip_a_header(tmp_path):
    path = tmp_path / "cases.csv"
    path.write_text('case_no\n"GC-1", GC-2;GC-3\nGC-1\n', encoding="utf-8")
    assert cli.case_list(["GC-0"], path) == ("GC-0", "GC-1", "GC-2", "GC-3")


def test_the_export_refuses_a_bad_window_a_sample_out_of_range_or_both_modes(tmp_path):
    out = str(tmp_path / "x.xlsx")
    for extra in (
        ["--admitted-from", "2026-11-16", "--admitted-to", "2026-11-15"],
        ["--sample", "5"],
        ["--sample", "20", "--case-no", "A"],
    ):
        with pytest.raises(SystemExit) as exit_:
            cli.main(["export", "routing", "--release", "r", "--out", out, *extra])
        assert exit_.value.code == 2


def test_a_list_outside_10_to_500_cases_is_refused_before_any_query(tmp_path, capsys):
    assert cli.main(["export", "routing", "--release", "r", "--case-no", "A", "--out", str(tmp_path / "x.xlsx")]) == 2
    assert "give between 10 and 500" in capsys.readouterr().err


def reviewed_workbook(tmp_path):
    from cfr_v2.evaluation import workbook as wb

    path = tmp_path / "routing_review.xlsx"
    rows = Routing().rows(run(), results(**STORED), FakeReader())
    wb.write(path, Routing().sheets({}), rows, lambda: [("Stage", "Routing (C04)"), ("Release", "r1")])
    book = load_workbook(path)
    book["routing"]["G2"] = "Y"
    book["routing"]["G3"] = "N"
    book["routing"]["H3"] = "servicing"
    book["routing"]["E4"] = "edited reasoning"
    book["about"]["B1"] = "Asha"
    book.save(path)
    return path


def test_score_writes_json_without_case_text_and_an_xlsx_with_the_reasons(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "flag_stale", lambda review, task: -1)  # no database here
    assert cli.main(["score", "routing", str(reviewed_workbook(tmp_path)), "--out-dir", str(tmp_path / "out")]) == 0
    report = json.loads((tmp_path / "out" / "routing_metrics.json").read_text())
    assert report["headline"]["label"] == "Correct vertical" and (
        report["headline"]["hits"],
        report["headline"]["n"],
    ) == (1, 2)
    assert report["about"]["reviewer"] == "Asha" and report["about"]["release"] == "r1"
    assert report["counts"]["skipped"] == 1 and "edited" in report["skipped"][0]["reason"]
    assert "late fee" not in json.dumps(report)  # counts and labels only
    book = load_workbook(tmp_path / "out" / "routing_metrics.xlsx")
    assert book.sheetnames[0] == "metrics" and "skipped" in book.sheetnames
    printed = capsys.readouterr().out
    assert "skipped: routing row 4" in printed and printed.strip().endswith(
        "Correct vertical 50.0% (n = 2, 9.5%–90.5%)"
    )


def test_score_refuses_a_workbook_exported_for_another_stage(tmp_path, capsys):
    assert cli.main(["score", "selection", str(reviewed_workbook(tmp_path)), "--out-dir", str(tmp_path)]) == 2
    assert "no sheet 'cases'" in capsys.readouterr().err


def test_stale_rows_are_flagged_from_the_results_stored_now(tmp_path, monkeypatch):
    from cfr_v2.evaluation import workbook as wb

    review = wb.read(reviewed_workbook(tmp_path), Routing().sheets())
    monkeypatch.setattr(cli, "current_refs", lambda db, task, runs: {"run-1": "sha256:other"})

    class DB:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    monkeypatch.setattr("cfr_v2.state.Database", DB)
    monkeypatch.setattr(
        "cfr_v2.runtime.settings.RuntimeSettings.from_env",
        classmethod(lambda cls: type("S", (), {"state_database_url": "x"})()),
    )
    assert cli.flag_stale(review, "c04.routing") == 2 and all(r.stale for r in review.rows["routing"])
