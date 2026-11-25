"""Selecting runs on the Cases page and re-running them from a stage (LDR-0045 step 2)."""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cfr_v2.console import cases, commands
from cfr_v2.console.app import create_app
from cfr_v2.console.jobs import JobStatus
from cfr_v2.console.settings import ConsoleSettings

REPO = Path(__file__).resolve().parents[3]
AT = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
ROWS = [cases.CaseRow(f"run-{i}", f"GC{i}", "cfr-v2-phase-e", "x", "batch", "PLAN_READY", AT, AT) for i in (1, 2)]


@pytest.fixture
def console(tmp_path, monkeypatch):
    env = dict(os.environ) | {
        "CFR_RELEASE_ID": "cfr-v2-phase-f",
        "CFR_STATE_DATABASE_URL": "postgresql://x@127.0.0.1:1/x",
    }
    app = create_app(ConsoleSettings(repo_root=REPO, workspace=tmp_path, env=env))
    monkeypatch.setattr(cases, "list_cases", lambda db, **kw: cases.CasePage(ROWS, True))
    started = {}

    def start(command, title, argv, **kwargs):
        folder = tmp_path / "job"
        (folder / "inputs").mkdir(parents=True, exist_ok=True)
        started.update(command=command, title=title, argv=list(argv(folder)), form=kwargs.get("form"), folder=folder)
        return type("J", (), {"id": "20261125-120000-abcd"})()

    return app, TestClient(app, base_url="http://127.0.0.1:8765"), started, start


def _csrf(app):
    return app.state.csrf


def test_the_list_offers_a_box_per_run_and_a_box_for_the_page(console):
    _, client, _, _ = console
    page = client.get("/cases?release=").text
    assert 'data-pick-all aria-label="Select every run on this page"' in page
    assert 'name="run" value="run-1" aria-label="Select GC1"' in page
    assert 'action="/cases/rerun"' in page and "Re-run from…" in page
    assert 'name="stage" value="c04.routing" data-mode checked' in page  # Routing by default
    assert "Select all runs matching these filters" in page and "select=all" in page  # there's another page
    assert 'New runs go under <span class="mono">cfr-v2-phase-f</span>' in page


def test_the_runs_ticked_go_to_the_job_as_a_file(console, monkeypatch):
    app, client, started, start = console
    monkeypatch.setattr(app.state.runner, "start", start)
    sent = client.post(
        "/cases/rerun",
        data={
            "csrf": _csrf(app),
            "release": "",
            "run": ["run-1", "run-2", "run-1"],
            "stage": "c02.pass2",
            "count_only": "on",
        },
        follow_redirects=False,
    )
    assert sent.status_code == 303 and sent.headers["location"] == "/jobs/20261125-120000-abcd"
    assert started["command"] == "cases.rerun" and started["title"] == "Re-run from Pass 2 · 2 runs · preview"
    argv = started["argv"]
    assert argv[argv.index("-m") + 1] == "cfr_v2.runtime.rerun" and argv[argv.index("--stage") + 1] == "c02.pass2"
    assert "--count-only" in argv and "--include-failed" not in argv
    runs = Path(argv[argv.index("--runs-file") + 1])
    assert runs.read_text() == "run-1\nrun-2\n"
    assert started["form"]["runs"] == "2" and "run" not in started["form"]  # the ids live in the file


def test_select_all_reads_every_run_the_filters_match_again_when_it_starts(console, monkeypatch):
    app, client, started, start = console
    monkeypatch.setattr(app.state.runner, "start", start)
    asked = []

    def matching(db, *, release, filters, facts=None):
        asked.append((release, filters.state))
        return cases.Matching(("a", "b", "c"), 0)

    monkeypatch.setattr(cases, "matching_runs", matching)
    page = client.get("/cases?release=cfr-v2-phase-e&state=PLAN_READY&select=all").text
    assert "All 3 runs matching these filters</strong> are selected" in page
    assert '<input type="hidden" name="all" value="1">' in page and "checked disabled" in page
    sent = client.post(
        "/cases/rerun",
        data={
            "csrf": _csrf(app),
            "all": "1",
            "release": "cfr-v2-phase-e",
            "state": "PLAN_READY",
            "stage": "c04.routing",
        },
        follow_redirects=False,
    )
    assert sent.status_code == 303 and asked[-1] == ("cfr-v2-phase-e", ("PLAN_READY",))
    assert Path(started["argv"][started["argv"].index("--runs-file") + 1]).read_text() == "a\nb\nc\n"
    assert "--count-only" not in started["argv"]  # unticked: queue them


def test_nothing_selected_is_sent_back_with_why(console):
    app, client, _, _ = console
    sent = client.post("/cases/rerun", data={"csrf": _csrf(app), "release": "", "stage": "c04.routing"})
    assert sent.status_code == 422 and "Select at least one case run, or choose Select all." in sent.text
    assert 'class="selection-bar fixed"' in sent.text  # the bar stays open, so the message shows


def test_a_re_run_is_started_only_from_the_cases_page(console):
    app, client, _, _ = console
    sent = client.post("/commands/cases.rerun", data={"csrf": _csrf(app), "stage": "c04.routing"})
    assert sent.status_code == 404


def test_after_a_preview_the_re_run_acts_on_the_same_runs(console, monkeypatch):
    app, client, started, start = console
    runner = app.state.runner

    def previewed(folder):
        (folder / "inputs").mkdir(parents=True, exist_ok=True)
        (folder / "inputs" / commands.RUNS_FILE).write_text("run-7\nrun-9\n")
        summary = "print('preview: 2 runs to re-run from c04.routing under cfr-v2-phase-f')"
        return [sys.executable, "-c", summary, "--count-only"]  # -c ignores it; next steps read it

    form = {"release": "", "stage": "c04.routing", "count_only": "on", "include_failed": "on", "runs": "2"}
    preview = runner.start("cases.rerun", "Re-run from Routing · 2 runs · preview", previewed, form=form)
    for _ in range(100):
        if runner.get(preview.id).status is not JobStatus.RUNNING:
            break
        time.sleep(0.05)

    links = dict(commands.BY_ID["cases.rerun"].next_links(runner.get(preview.id)))
    assert list(links) == ["Re-run them"]
    page = client.get(links["Re-run them"].split("#")[0]).text
    assert "<strong>2 runs</strong> from" in page and f'name="from_job" value="{preview.id}"' in page
    assert 'id="cases.rerun-count_only" name="count_only" aria-describedby' in page  # Preview only: off this time
    assert 'id="cases.rerun-include_failed" name="include_failed" checked aria-describedby' in page  # kept

    monkeypatch.setattr(runner, "start", start)
    sent = client.post(
        "/cases/rerun",
        data={"csrf": _csrf(app), "from_job": preview.id, "release": "", "stage": "c04.routing"},
        follow_redirects=False,
    )
    assert sent.status_code == 303
    assert Path(started["argv"][started["argv"].index("--runs-file") + 1]).read_text() == "run-7\nrun-9\n"
    assert client.get(f"/cases?again={preview.id}").status_code == 200  # Run Again reuses them too
    assert client.get("/cases?again=20991231-000000-zzzz").status_code == 404
