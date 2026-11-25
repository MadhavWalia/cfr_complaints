"""The command catalogue (LDR-0023 step 3): form values become the terminal's exact arguments, bad
values come back named, and every area's forms render and run."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cfr_v2.console import commands
from cfr_v2.console.app import AREAS, create_app
from cfr_v2.console.commands import BY_ID, CATALOGUE, Context, FormError
from cfr_v2.console.jobs import Job, JobStatus
from cfr_v2.console.settings import ConsoleSettings

REPO = Path(__file__).resolve().parents[3]


def _args(command_id: str, form: dict, ctx: Context, folder: Path) -> list[str]:
    command = BY_ID[command_id]
    argv = command.argv(command.parse(form, ctx), folder)
    assert argv[:3] == [sys.executable, "-m", command.module]
    return argv[3:]


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "pdfs").mkdir()
    for folder in ("store", "lightrag_store_built"):  # a store elsewhere, and the snapshot's own
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "cfr_store.json").write_text(json.dumps({"procedures_ref": "sha256:built"}))
    (tmp_path / "config" / "verticals").mkdir(parents=True)
    (tmp_path / commands.VERTICALS_DOCUMENT).write_text("verticals")
    (tmp_path / "registry" / "releases").mkdir(parents=True)
    (tmp_path / "registry" / "releases" / "cfr-v2-phase-d.yaml").write_text("")
    snap = Job(
        "20261112-110000-bbbb",
        "procedures.snapshot",
        "Snapshot",
        ("x",),
        JobStatus.DONE,
        "2026-11-12T11:00:00+00:00",
        summary="sha256:new",
    )
    done = {"procedures.snapshot": [snap]}
    return Context(
        root=tmp_path,
        env={},
        done_jobs=lambda command: done.get(command, []),
        outputs=lambda job_id: tmp_path / "jobs" / job_id / "outputs",
    )


def test_the_catalogue_is_well_formed():
    areas = {path[1:] for path, _ in AREAS}
    assert len(BY_ID) == len(CATALOGUE)
    for command in CATALOGUE:
        assert command.id.split(".")[0] == command.area and command.area in areas
        assert command.effect.endswith(".") and command.action
        assert len({f.name for f in command.fields}) == len(command.fields)


def test_intake_commands(ctx, tmp_path):
    out = str(tmp_path / "outputs" / "missing.txt")
    assert _args("intake.window", {"start_date": "2026-09-01", "end_date": "2026-09-07"}, ctx, tmp_path) == [
        "--start-date", "2026-09-01", "--end-date", "2026-09-07",
    ]  # fmt: skip
    with pytest.raises(FormError, match="To: on or after From"):
        BY_ID["intake.window"].parse({"start_date": "2026-09-07", "end_date": "2026-09-01"}, ctx)
    sheet = tmp_path / "inputs" / "cases.xlsx"
    values = BY_ID["intake.case_list"].parse({"cases": SimpleNamespace(filename="cases.xlsx"), "sheet": "Q3"}, ctx)
    values["cases"] = sheet  # saved by the console before the arguments are built
    assert BY_ID["intake.case_list"].argv(values, tmp_path)[3:] == [
        "--case-nos-from", str(sheet), "--column", "case_no", "--sheet", "Q3", "--missing-out", out,
    ]  # fmt: skip
    with pytest.raises(FormError, match="Spreadsheet: choose a file"):
        BY_ID["intake.case_list"].parse({"cases": SimpleNamespace(filename="")}, ctx)
    assert _args("intake.synthetic", {"count": "3", "seed": "9"}, ctx, tmp_path) == ["--synthetic", "3", "--seed", "9"]
    assert BY_ID["intake.synthetic"].describe({"count": 3, "seed": 9}) == "Intake · 3 synthetic cases, seed 9"
    with pytest.raises(FormError, match="Cases: at least 1. Seed: a whole number"):
        BY_ID["intake.synthetic"].parse({"count": "0", "seed": "x"}, ctx)
    assert _args("intake.sql", {"by_case_no": "on"}, ctx, tmp_path) == ["--print-sql", "--case-nos-from", "cases.xlsx"]
    assert _args("intake.sql", {}, ctx, tmp_path) == ["--print-sql"]


def test_procedures_commands_take_paths_and_default_to_the_store(ctx, tmp_path):
    pdfs, store = str(tmp_path / "pdfs"), str(tmp_path / "store")
    assert _args("procedures.inspect", {"folder": "pdfs"}, ctx, tmp_path) == [
        "inspect", pdfs, "--out", str(tmp_path / "outputs" / "inspect.json"),
    ]  # fmt: skip
    with pytest.raises(FormError, match="PDF folder: no folder at nowhere"):
        BY_ID["procedures.inspect"].parse({"folder": "nowhere"}, ctx)
    assert _args("procedures.snapshot", {"folder": pdfs, "previous": "sha256:old"}, ctx, tmp_path) == [
        "snapshot", pdfs, "--previous", "sha256:old",
    ]  # fmt: skip
    # a build defaults to the newest snapshot made here, into that snapshot's own folder
    assert _args("procedures.build", {}, ctx, tmp_path) == [
        "lightrag-build",
        "sha256:new",
        "--concurrency",
        "16",
        "--save-every",
        "64",
    ]
    # an older job's form, run again, has no save_every: left empty, it takes the default
    assert _args("procedures.build", {"save_every": ""}, ctx, tmp_path)[-2:] == ["--save-every", "64"]
    assert _args("procedures.build", {"save_every": "1"}, ctx, tmp_path)[-2:] == ["--save-every", "1"]
    assert _args("procedures.build", {"procedures": "sha256:built"}, ctx, tmp_path)[1] == "sha256:built"  # resumes
    with pytest.raises(FormError, match="store was built from sha256:built…, and a store holds one snapshot. Leave"):
        BY_ID["procedures.build"].parse({"workdir": store}, ctx)
    resume = {"procedures": "sha256:built", "workdir": store}
    assert _args("procedures.build", resume, ctx, tmp_path)[2:4] == ["--workdir", store]  # a folder elsewhere
    with pytest.raises(FormError, match="Snapshot: a ref such as sha256"):
        BY_ID["procedures.build"].parse({"procedures": "../etc"}, ctx)
    # CFR_LIGHTRAG_WORKDIR overrides the folder, so a store holding another snapshot is named
    overridden = Context(ctx.root, {"CFR_LIGHTRAG_WORKDIR": "store"}, ctx.done_jobs, ctx.outputs)
    with pytest.raises(
        FormError, match="CFR_LIGHTRAG_WORKDIR in .env chooses that folder: remove it to use lightrag_store_new"
    ):
        BY_ID["procedures.build"].parse({}, overridden)
    # a search needs a store built from its snapshot
    search = {"query": "fee after cancel", "procedures": "sha256:built", "text": "on"}
    assert _args("procedures.search", search, ctx, tmp_path) == [
        "search", "sha256:built", "fee after cancel", "--limit", "8", "--text",
    ]  # fmt: skip
    with pytest.raises(FormError, match="there is no store for it at lightrag_store_new. Build it first"):
        BY_ID["procedures.search"].parse(search | {"procedures": "sha256:new"}, ctx)


def test_the_snapshot_starts_as_the_current_releases():
    ctx = Context(REPO, {"CFR_RELEASE_ID": "cfr-v2-phase-e"}, lambda command: [], lambda job_id: REPO)
    pinned = commands.ReleaseRegistry(REPO / "registry").load("cfr-v2-phase-e").procedure_snapshot
    assert pinned and BY_ID["procedures.search"].fields[1].initial(ctx) == pinned
    assert BY_ID["procedures.build"].fields[0].initial(ctx) == pinned  # no snapshot made here yet
    assert commands._release_snapshot(Context(REPO, {"CFR_RELEASE_ID": "nope"}, ctx.done_jobs, ctx.outputs)) is None


def test_routing_commands(ctx, tmp_path):
    assert _args("routing.publish", {}, ctx, tmp_path) == [
        "publish-verticals", str(tmp_path / commands.VERTICALS_DOCUMENT),
    ]  # fmt: skip
    (tmp_path / "outputs").mkdir()
    backfill = _args("routing.backfill", {"from_release": "cfr-v2-phase-d", "count_only": "on"}, ctx, tmp_path)
    assert backfill == [
        "backfill", "--from-release", "cfr-v2-phase-d",
        "--concurrency", "4", "--count-only", "--out", str(tmp_path / "outputs" / "backfill.jsonl"),
    ]  # fmt: skip
    with pytest.raises(FormError, match="Failed under: choose one of the options"):
        BY_ID["routing.backfill"].parse({"from_release": "cfr-v2-phase-z"}, ctx)


def test_evaluation_review_and_system_commands(ctx, tmp_path):
    out = tmp_path / "outputs"
    today = date.today().isoformat()
    release = {"release": "cfr-v2-phase-d"}
    assert _args("evaluation.routing_export", release, ctx, tmp_path) == [
        "export", "routing", "--release", "cfr-v2-phase-d", "--out", str(out / f"routing_review_cfr-v2-phase-d_{today}.xlsx"),
    ]  # fmt: skip
    sample = _args(
        "evaluation.pass1_export",
        release | {"which": "sample", "sample": "50", "seed": "7", "admitted_from": "2026-11-01"},
        ctx,
        tmp_path,
    )
    assert sample[:9] == [
        "export",
        "pass1",
        "--release",
        "cfr-v2-phase-d",
        "--admitted-from",
        "2026-11-01",
        "--sample",
        "50",
        "--seed",
    ]
    listed = " ".join(f"GC-{i}" for i in range(12))
    chosen = _args(
        "evaluation.selection_export", release | {"which": "chosen", "case_nos": listed, "sample": "50"}, ctx, tmp_path
    )
    assert (
        chosen[4:8] == ["--case-no", "GC-0", "--case-no", "GC-1"] and "--sample" not in chosen
    )  # only the mode's fields
    assert "--case-no" not in _args(
        "evaluation.intake_export", release | {"case_nos": listed}, ctx, tmp_path
    )  # mode: all
    with pytest.raises(FormError, match="Case numbers: 2 given; between 10 and 500"):
        BY_ID["evaluation.pass2_export"].parse(release | {"which": "chosen", "case_nos": "A B"}, ctx)
    with pytest.raises(FormError, match="Chosen cases: type the case numbers or choose a file, not both"):
        BY_ID["evaluation.pass2_export"].parse(release | {"which": "chosen"}, ctx)
    with pytest.raises(FormError, match="How many: at least 10"):
        BY_ID["evaluation.pass2_export"].parse(release | {"which": "sample", "sample": "5"}, ctx)
    with pytest.raises(FormError, match="Admitted to: on or after Admitted from"):
        BY_ID["evaluation.pass2_export"].parse(
            release | {"admitted_from": "2026-11-02", "admitted_to": "2026-11-01"}, ctx
        )
    with pytest.raises(FormError, match="Reviewed workbook: choose a file"):
        BY_ID["evaluation.routing_score"].parse({}, ctx)
    book = SimpleNamespace(filename="r.xlsx")
    assert _args("evaluation.routing_score", {"workbook": book}, ctx, tmp_path)[:2] == ["score", "routing"]
    assert BY_ID["evaluation.routing_score"].page == "/evaluation/routing"
    assert _args("system.worker", {"max_queued": "25"}, ctx, tmp_path) == [
        "--lanes", "batch,realtime", "--max-queued", "25", "--drain-timeout", "30",
    ]  # fmt: skip
    assert BY_ID["system.worker"].long_running
    assert _args("system.smoke", {"check": "embeddings-load"}, ctx, tmp_path) == ["--embeddings-load"]
    assert _args("system.migrate", {}, ctx, tmp_path) == []
    assert _args("system.tests", {}, ctx, tmp_path) == ["-q", "tests/unit", "tests/telemetry"]


def test_summaries_read_what_the_command_printed(tmp_path):
    (tmp_path / "outputs" / "c02_trial").mkdir(parents=True)
    (tmp_path / "log.txt").write_text(
        "[1/2] a.pdf\n\nsnapshot: sha256:abc\n========= 581 passed, 59 skipped in 31.20s =========\n"
    )
    assert BY_ID["procedures.snapshot"].summarize(tmp_path, 0) == "sha256:abc"
    assert BY_ID["system.tests"].summarize(tmp_path, 0) == "581 passed, 59 skipped in 31.20s"
    (tmp_path / "log.txt").write_text(
        "1,204 cases exported, 3,310 rows, to r.xlsx\nCorrect vertical 87.4% (n = 412, 84.0%–90.4%)\n"
    )
    assert BY_ID["evaluation.routing_export"].summarize(tmp_path, 0) == "1,204 cases exported, 3,310 rows"
    assert BY_ID["evaluation.routing_score"].summarize(tmp_path, 0) == "Correct vertical 87.4% (n = 412, 84.0%–90.4%)"


def _wait(runner, job_id: str, timeout: float = 30.0) -> Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = runner.get(job_id)
        if job.finished:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


@pytest.fixture
def console(tmp_path):
    env = dict(os.environ) | {
        "CFR_RELEASE_ID": "cfr-v2-phase-e",
        "CFR_STATE_DATABASE_URL": "postgresql://x@127.0.0.1:1/x",
    }
    app = create_app(ConsoleSettings(repo_root=REPO, workspace=tmp_path, env=env))
    return app, TestClient(app, base_url="http://127.0.0.1:8765")


def test_every_area_renders_its_forms(console):
    _, client = console
    for path, _ in AREAS:
        assert client.get(path).status_code == 200, path
    system = client.get("/system").text
    assert "Queue" in system and "Start the Worker" in system and "Can't read the state database" in system
    for tab in ("intake", "pass1", "pass2", "selection", "routing", "planning"):
        assert client.get(f"/evaluation/{tab}").status_code == 200, tab
    assert client.get("/evaluation/nope").status_code == 404
    assert 'id="procedures.sources_export"' in client.get("/procedures").text


def test_offline_commands_run_for_real_and_their_files_download(console):
    """Commands that need no database or gateway: an evidence-source export and the registry check;
    and a review export sent back with what to fix."""
    app, client = console
    csrf = app.state.csrf

    def run(command_id: str, data: dict, files=None) -> Job:
        response = client.post(
            f"/commands/{command_id}", data={"csrf": csrf, **data}, files=files, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        return _wait(app.state.runner, response.headers["location"].rsplit("/", 1)[-1])

    export = run("procedures.sources_export", {"version": "evidence-sources-1"})
    assert export.status is JobStatus.DONE
    assert [name for name, _ in app.state.runner.files(export.id, "outputs")] == ["evidence-sources-1.xlsx"]
    assert run("procedures.sources_check", {}).status is JobStatus.DONE

    bad = client.post(
        "/commands/evaluation.routing_export", data={"csrf": csrf, "release": "cfr-v2-phase-e", "which": "chosen"}
    )
    assert bad.status_code == 422 and "Chosen cases: type the case numbers or choose a file" in bad.text
    assert 'aria-current="page">Routing</a>' in bad.text  # sent back to its own tab

    # a job's files are deleted with it
    assert client.post(f"/jobs/{export.id}/delete", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert client.get(f"/jobs/{export.id}").status_code == 404
    assert client.post(f"/jobs/{export.id}/delete").status_code == 403  # no token


def test_snapshots_are_offered_with_where_they_were_seen_and_their_store(ctx, tmp_path):
    (tmp_path / "registry" / "releases" / "cfr-v2-phase-x.yaml").write_text("procedure_snapshot: sha256:built\n")
    choices = dict(commands.known_snapshots(ctx))
    assert list(choices) == ["sha256:new", "sha256:built"]
    assert "taken" in choices["sha256:new"] and choices["sha256:new"].endswith("no store yet")
    assert "pinned by cfr-v2-phase-x" in choices["sha256:built"] and choices["sha256:built"].endswith("store built")
    # "Another ref…" reads the typed one; a ref must look like one
    form = {"procedures": commands.OTHER, "procedures_other": "sha256:built"}
    assert BY_ID["procedures.build"].parse(form, ctx)["procedures"] == "sha256:built"
    with pytest.raises(FormError, match="Snapshot: a ref such as sha256"):
        BY_ID["procedures.build"].parse({"procedures": "latest"}, ctx)


def test_a_finished_job_links_to_what_usually_follows(ctx):
    snapshot = Job("20261112-121420-9af6", "procedures.snapshot", "Snapshot", ("x",), JobStatus.DONE,
                   "2026-11-12T12:14:20+00:00", summary="sha256:" + "b" * 64)  # fmt: skip
    [(label, href)] = BY_ID["procedures.snapshot"].next_links(snapshot)
    assert label == "Build its LightRAG store"
    assert (
        href == f"/procedures?fill=procedures.build&from={snapshot.id}&procedures=sha256%3A{'b' * 64}#procedures.build"
    )
    argv = ("python", "-m", "cfr_v2.procedures", "lightrag-build", "sha256:" + "b" * 64, "--concurrency", "16")
    build = Job("20261112-130000-aaaa", "procedures.build", "Build", argv, JobStatus.DONE, "2026-11-12T13:00:00+00:00")
    links = dict(BY_ID["procedures.build"].next_links(build))
    assert links["Make a release with this snapshot"] == f"/releases?snapshot=sha256%3A{'b' * 64}#new-release"
    assert BY_ID["procedures.build"].next_links(replace(build, status=JobStatus.FAILED)) == []  # only when done
    count = Job("20261112-140000-cccc", "routing.backfill", "Backfill · count", ("--count-only",), JobStatus.DONE,
                "2026-11-12T14:00:00+00:00", form={"from_release": "cfr-v2-phase-d", "count_only": "on"})  # fmt: skip
    [(label, href)] = BY_ID["routing.backfill"].next_links(count)
    assert label == "Route them" and "from_release=cfr-v2-phase-d" in href and "count_only=#" in href  # unticked
    assert BY_ID["routing.backfill"].next_links(replace(count, argv=("backfill",))) == []  # a real run: done


def test_every_command_says_what_it_does_and_counts_calls_where_it_can(ctx, tmp_path):
    for command in CATALOGUE:
        assert all(tag in commands.TAGS for tag in command.tags), command.id
        if "calls gpt-5" not in command.tags:
            assert "calls the models" not in command.effect, command.id
    assert set(BY_ID["procedures.build"].tags) == {"calls gpt-5", "long-running"}
    assert BY_ID["procedures.build"].estimate(ctx) is None  # no snapshot log to count sections from
    log = tmp_path / "jobs" / "20261112-110000-bbbb" / "log.txt"
    log.parent.mkdir(parents=True)
    log.write_text("35 distinct files\ncurrent: 2,016 pages; 1,844 sections; 3,508,141 characters\n")
    build = BY_ID["procedures.build"].estimate(ctx)
    assert build.calls == 5532 and build.render().startswith("About 5,532 gpt-5 calls for the newest snapshot: 1,844")
    assert BY_ID["routing.backfill"].estimate(ctx) is None  # no count yet


def test_number_fields_are_steppers_and_uploads_are_split_buttons(console):
    from cfr_v2.console.commands import Field

    assert Field("n", "N", "int", minimum=1, maximum=500).stepper
    assert Field("n", "N", "int", minimum=1).stepper  # unbounded: minus stops at the minimum only
    assert Field("x", "X", "float").stepper
    assert not Field("t", "T").stepper
    _, client = console
    page = client.get("/evaluation/routing").text
    assert "data-stepper" in page and 'step="10"' in page and "Between 10 and 500." in page
    assert 'aria-label="Increase How many"' in page
    assert "data-upload" in page and "Choose File</button>" in page and "Remove File" in page
    assert "Accepts .xlsx." in page
    assert 'step="any"' in client.get("/system").text  # a float with no step of its own


def test_dropdowns_are_single_select_panels(console):
    _, client = console
    page = client.get("/evaluation/routing").text
    assert "<select" not in page
    assert '<details class="ms single">' in page and 'role="radiogroup"' in page
    assert 'type="radio"' in page


def test_date_fields_are_text_with_the_calendar_button(console):
    _, client = console
    page = client.get("/intake").text
    assert 'type="date"' not in page  # the browser's picker is replaced by the console's calendar
    assert 'pattern="\\d{4}-\\d{2}-\\d{2}"' in page and "(YYYY-MM-DD)" in page
    assert 'data-datepicker data-label="a date for From" aria-label="Choose a date for From"' in page
    assert '<script type="module" src="' in page and "/static/datepicker.js?v=" in page
