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


# --- retire and restore (LDR-0045 step 3) -----------------------------------------------------


def test_retire_is_offered_and_restore_only_with_retired_runs_in_view(console):
    _, client, _, _ = console
    page = client.get("/cases?release=").text
    assert 'name="case_action" value="cases.retire"' in page and "Retire…" in page
    assert 'id="cases.restore"' not in page  # retired runs are hidden, so nothing to restore
    assert 'name="retired"' in page and "Retired runs" in page  # the filter, under More filters
    shown = client.get("/cases?release=&retired=show").text
    assert 'id="cases.restore"' in shown and 'id="cases.retire"' in shown
    assert "<span>Retired runs shown</span>" in shown
    only = client.get("/cases?release=&retired=only").text
    assert 'id="cases.restore"' in only and 'id="cases.retire"' not in only


def test_retiring_the_ticked_runs_starts_a_job_with_the_reason(console, monkeypatch):
    app, client, started, start = console
    monkeypatch.setattr(app.state.runner, "start", start)
    sent = client.post(
        "/cases/retire",
        data={"csrf": _csrf(app), "release": "", "run": ["run-1", "run-2"], "reason": "phase-d runs"},
        follow_redirects=False,
    )
    assert sent.status_code == 303 and started["command"] == "cases.retire"
    argv = started["argv"]
    assert argv[argv.index("-m") + 1 : argv.index("-m") + 3] == ["cfr_v2.runtime.retire", "retire"]
    assert argv[argv.index("--reason") + 1] == "phase-d runs" and started["title"] == "Retire · 2 runs"
    assert Path(argv[argv.index("--runs-file") + 1]).read_text() == "run-1\nrun-2\n"


def test_restoring_from_the_case_page_starts_a_job_for_that_run(console, monkeypatch):
    app, client, started, start = console
    monkeypatch.setattr(app.state.runner, "start", start)
    sent = client.post("/cases/restore", data={"csrf": _csrf(app), "run": "run-9"}, follow_redirects=False)
    assert sent.status_code == 303 and started["argv"][-3:] == ["restore", "--runs-file", started["argv"][-1]]
    assert Path(started["argv"][-1]).read_text() == "run-9\n"
    assert client.post("/cases/nothing", data={"csrf": _csrf(app), "run": "run-9"}).status_code == 404


def test_a_retired_row_has_a_badge_and_the_filter_reaches_the_query(console, monkeypatch):
    _, client, _, _ = console
    asked = []
    row = cases.CaseRow("run-3", "GC3", "cfr-v2-phase-d", "x", "batch", "PLAN_READY", AT, AT, retired=True)

    def listed(db, **kw):
        asked.append(kw["filters"].retired)
        return cases.CasePage([row], False)

    monkeypatch.setattr(cases, "list_cases", listed)
    page = client.get("/cases?release=&retired=show").text
    assert asked[-1] == "show" and '<span class="badge">Retired</span>' in page
    client.get("/cases?release=&retired=everything")
    assert asked[-1] is None  # not an option: hidden, the default


def test_the_retired_filter_is_in_the_sql_with_hidden_as_the_default():
    sql = cases.CASES_SQL
    assert "LEFT JOIN case_run_retired x" in sql and "(%(retired)s::text = 'only')" in sql
    assert cases.CaseFilters.parse({}).retired is None
    assert cases.CaseFilters.parse({"retired": "only"}).query() == {"retired": "only"}


def test_the_case_page_says_a_run_is_retired_or_a_re_run_and_offers_restore(console, monkeypatch):
    from cfr_v2.artifacts import InMemoryArtifactStore

    app, client, _, _ = console
    app.state.store = InMemoryArtifactStore()
    row = cases.CaseRow("run-5", "GC5", "cfr-v2-phase-f", "x", "batch", "PLAN_READY", AT, AT)
    admitted = cases.Event("ADMITTED", AT, {"rerun_of": "run-0000-source", "from": "c04.routing"})
    run = cases.CaseRun(row, (admitted,), (), None, (AT, "phase-d runs"))
    monkeypatch.setattr(cases, "read_case", lambda db, case_run_id: run if case_run_id == "run-5" else None)
    page = client.get("/cases/run-5").text
    assert 'A re-run of <a class="mono" href="/cases/run-0000-source">' in page and "c04.routing" in page
    assert "Retired <time" in page and ": phase-d runs</strong>" in page
    assert 'action="/cases/restore"' in page and 'name="run" value="run-5"' in page


# --- purge (LDR-0045 step 4) ----------------------------------------------------------------------


def test_purge_is_offered_only_with_retired_runs_in_view_and_warns_first(console):
    _, client, _, _ = console
    assert 'value="cases.purge"' not in client.get("/cases?release=").text
    page = client.get("/cases?release=&retired=only").text
    assert 'name="case_action" value="cases.purge"' in page and "Purge…" in page
    assert '<div class="notice critical" role="note"><strong>Purging can&#39;t be undone.</strong>' in page
    assert "Purge for good" in page and 'name="confirm"' in page


def test_purging_needs_the_count_typed_and_then_starts_the_job(console, monkeypatch):
    app, client, started, start = console
    monkeypatch.setattr(app.state.runner, "start", start)
    data = {"csrf": _csrf(app), "release": "", "retired": "only", "run": ["run-1", "run-2"], "reason": "phase-d"}
    wrong = client.post("/cases/purge", data={**data, "confirm": "3"})
    assert wrong.status_code == 422 and "Purge: type 2, the number of runs selected, to confirm." in wrong.text
    assert "Nothing was deleted." in wrong.text and not started
    sent = client.post("/cases/purge", data={**data, "confirm": " 2 "}, follow_redirects=False)
    assert sent.status_code == 303 and started["command"] == "cases.purge"
    argv = started["argv"]
    assert argv[argv.index("-m") + 2] == "purge" and argv[argv.index("--confirm") + 1] == "2"
    assert argv[argv.index("--reason") + 1] == "phase-d" and started["title"] == "Purge · 2 runs"
