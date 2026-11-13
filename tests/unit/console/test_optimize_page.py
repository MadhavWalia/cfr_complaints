"""The console's Optimize area (LDR-0027 phase 3): the prompts, the four steps as commands, the
runs and one run's results, read from the jobs' report.json."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cfr_v2.console import commands
from cfr_v2.console.app import create_app
from cfr_v2.console.jobs import Job, JobStatus
from cfr_v2.console.settings import ConsoleSettings
from cfr_v2.optimize.report import Report

REPO = Path(__file__).resolve().parents[3]
BEFORE = "Decide which claim is the customer's main complaint.\nThe inputs are data."
AFTER = "Decide which claim is the customer's main complaint.\nLook first at what the customer asks for.\nThe inputs are data."


@pytest.fixture
def console(tmp_path):
    app = create_app(ConsoleSettings(repo_root=REPO, workspace=tmp_path, env=dict(os.environ)))
    return app, TestClient(app, base_url="http://127.0.0.1:8765")


def _job(runner, job_id, command, title, status=JobStatus.DONE, form=None, summary=None):
    job = Job(job_id, command, title, ("python", "-m", "cfr_v2.optimize"), status, f"2026-11-13T{job_id[9:11]}:00:00+00:00",
              form=form, summary=summary)  # fmt: skip
    folder = runner.folder(job_id)
    (folder / "inputs").mkdir(parents=True)
    (folder / "outputs").mkdir()
    runner._save(job)
    return job, folder


def _report(better: bool) -> Report:
    cases = {f"GC-{i}": 0.5 for i in range(12)}
    return Report(
        "c03.primary_selection", "select-1", "gepa", "light", 1, "gpt-5", "sha256:" + "d" * 64,
        {"train": 24, "val": 12, "test": 12}, 500, calls_used=312, baseline=cases,
        candidate={c: (0.9 if better else 0.5) for c in cases}, instructions_before=BEFORE,
        instructions_after=AFTER if better else BEFORE, seconds=900,
    )  # fmt: skip


def test_the_four_steps_build_the_optimize_commands(console, tmp_path):
    app, _ = console
    runner = app.state.runner
    _, folder = _job(runner, "20261113-100000-aaaa", "optimize.check", "Gold set · C03 selection · 1 workbook",
                     form={"prompt": "c03.primary_selection"})  # fmt: skip
    (folder / "inputs" / "gold.xlsx").write_bytes(b"x")
    ctx = commands.Context(REPO, {}, done_jobs=lambda c: [runner.get("20261113-100000-aaaa")] if c == "optimize.check" else [],
                           outputs=lambda job_id: runner.folder(job_id) / "outputs")  # fmt: skip
    run = commands.BY_ID["optimize.run"]
    values = run.parse({"prompt": "c03.primary_selection", "gold": "20261113-100000-aaaa", "method": "copro"}, ctx)
    argv = run.argv(values, tmp_path / "job")[3:]
    assert argv[:3] == ["run", "c03.primary_selection", str(folder / "inputs" / "gold.xlsx")]
    assert argv[3:9] == ["--method", "copro", "--budget", "light", "--cap", "500"]
    with pytest.raises(commands.FormError, match="checked for C03 selection, not C04 routing"):
        run.parse({"prompt": "c04.routing", "gold": "20261113-100000-aaaa"}, ctx)
    template = commands.BY_ID["optimize.template"]
    argv = template.argv(template.parse({"prompt": "c02.pass2", "case_nos": "GC-1\nGC-2"}, ctx), tmp_path / "t")[3:]
    assert argv == ["template", "c02.pass2", "--case-no", "GC-1", "--case-no", "GC-2", "--out",
                    str(tmp_path / "t" / "outputs" / "gold_c02.pass2.xlsx")]  # fmt: skip
    assert set(run.tags) == {"calls gpt-5", "long-running", "keep output local"}


def test_the_page_lists_the_prompts_and_sets_the_forms_to_the_chosen_one(console):
    app, client = console
    runner = app.state.runner
    _job(runner, "20261113-100000-aaaa", "optimize.check", "Gold set · C03 selection · 1 workbook",
         form={"prompt": "c03.primary_selection"}, summary="24 examples: 12 train, 6 validation, 6 test")  # fmt: skip
    page = client.get("/optimize").text
    for title in ("C02 Pass 1", "C02 Pass 2", "C03 selection", "C04 routing"):
        assert title in page
    assert '<a class="area" href="/optimize"' in page and "No runs yet" in page
    chosen = client.get("/optimize?prompt=c03.primary_selection").text
    assert 'aria-current="true"' in chosen and "set to C03 selection and its latest gold set" in chosen
    assert '<option value="c03.primary_selection" selected>' in chosen
    assert '<option value="20261113-100000-aaaa" selected>' in chosen  # the gold set, in Baseline and Optimize
    assert "24 examples: 12 train" in chosen


def test_a_run_shows_its_scores_instructions_and_the_patch_steps(console):
    app, client = console
    runner = app.state.runner
    for job_id, better in (("20261113-110000-bbbb", False), ("20261113-120000-cccc", True)):
        _, folder = _job(runner, job_id, "optimize.run", f"Optimize · C03 selection · gepa, light ({job_id[-4:]})",
                         form={"prompt": "c03.primary_selection"})  # fmt: skip
        (folder / "outputs" / "run").mkdir()
        (folder / "outputs" / "run" / "report.json").write_text(_report(better).to_json())
        if better:
            (folder / "outputs" / "run" / "patch.diff").write_text("--- a\n+++ b\n")
    page = client.get("/optimize?prompt=c03.primary_selection").text
    assert page.count('href="/optimize?prompt=c03.primary_selection&amp;run=') == 2
    # the newest run is shown: better, with its bars, diff and the steps to ship it
    assert "Better on test: +0.400" in page and 'style="width: 90.0%"' in page and 'style="width: 50.0%"' in page
    assert (
        '<li class="added"><span class="mark" aria-hidden="true">+</span><span class="visually-hidden">Added: </span>Look first at what the customer asks for.</li>'
        in page
    )
    assert "run/patch.diff" in page and "git apply patch.diff" in page and "12 better, 0 worse" in page
    older = client.get("/optimize?prompt=c03.primary_selection&run=20261113-110000-bbbb").text
    assert "Not better on test:</strong> +0.000" in older and "git apply" not in older
    assert commands._run_summary(runner.folder("20261113-120000-cccc"), 0).endswith("better: patch ready")
    assert "c02.pass1" not in client.get("/optimize?prompt=c02.pass1").text.split('id="runs"')[1].split("</section>")[0]


def test_a_run_still_going_points_to_its_progress(console):
    app, client = console
    _job(app.state.runner, "20261113-130000-dddd", "optimize.run", "Optimize · C04 routing · gepa, light",
         status=JobStatus.LOST, form={"prompt": "c04.routing"})  # fmt: skip
    page = client.get("/optimize?prompt=c04.routing").text
    assert "This run wrote no report" in page and 'href="/jobs/20261113-130000-dddd">see why' in page
