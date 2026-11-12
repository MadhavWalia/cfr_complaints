"""Console jobs (LDR-0023 step 2): real subprocesses, their logs and files, stopping, restarts,
and the path from a form to a finished job."""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cfr_v2.console import formats
from cfr_v2.console.app import create_app
from cfr_v2.console.jobs import Job, JobRunner, JobStatus
from cfr_v2.console.settings import ConsoleSettings


def _wait(runner: JobRunner, job_id: str, timeout: float = 20.0) -> Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = runner.get(job_id)
        if job.finished:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


def _python(code: str):
    return lambda folder: [sys.executable, "-c", code]


@pytest.fixture
def runner(tmp_path):
    return JobRunner(tmp_path / "jobs", cwd=tmp_path, env=dict(os.environ))


def test_a_job_runs_logs_and_ends_with_its_summary(runner):
    job = runner.start(
        "test.echo",
        "Echo",
        _python("import sys; print('hello'); print('oops', file=sys.stderr)"),
        summarize=lambda folder, code: f"exit {code}",
    )
    assert job.status is JobStatus.RUNNING and job.pid
    done = _wait(runner, job.id)
    assert (done.status, done.exit_code, done.summary) == (JobStatus.DONE, 0, "exit 0")
    assert runner.log_path(job.id).read_text().splitlines() == ["hello", "oops"]  # stdout and stderr, in order
    assert runner.list()[0].id == job.id


def test_a_failing_job_is_failed_and_a_stopped_one_stopped(runner):
    failed = _wait(runner, runner.start("t", "Fails", _python("raise SystemExit(3)")).id)
    assert (failed.status, failed.exit_code) == (JobStatus.FAILED, 3)
    slow = runner.start("t", "Sleeps", _python("import time; time.sleep(30)"))
    runner.stop(slow.id)
    assert _wait(runner, slow.id).status is JobStatus.STOPPED


def test_a_failed_job_records_why_and_the_form_it_was_started_from(runner):
    code = "raise RuntimeError('the store was built from another snapshot')"
    job = runner.start("t", "Fails", _python(code), form={"procedures": "sha256:new", "workdir": ""})
    failed = _wait(runner, job.id)
    assert failed.reason == "RuntimeError: the store was built from another snapshot"
    assert failed.form == {"procedures": "sha256:new", "workdir": ""}
    assert _wait(runner, runner.start("t", "Fine", _python("print(1)")).id).reason is None  # only on failure


def test_force_stop_kills_a_job_that_ignores_sigterm(runner):
    stubborn = runner.start(
        "t",
        "Ignores SIGTERM",
        _python(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"
        ),
    )
    deadline = time.monotonic() + 10
    while "ready" not in runner.log_path(stubborn.id).read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    runner.stop(stubborn.id)
    time.sleep(0.3)
    assert runner.get(stubborn.id).status is JobStatus.RUNNING  # it ignored the polite request
    runner.stop(stubborn.id, force=True)
    assert _wait(runner, stubborn.id).status is JobStatus.STOPPED


def test_a_restarted_console_adopts_live_jobs_and_marks_dead_ones_lost(tmp_path, monkeypatch):
    """Records a previous console left behind: one process still running, one long gone."""
    import subprocess

    import cfr_v2.console.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "ADOPTED_POLL_SEC", 0.05)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    root = tmp_path / "jobs"
    for job_id, pid in (("20261112-090000-aaaa", process.pid), ("20261112-090001-bbbb", 9_999_999)):
        (root / job_id).mkdir(parents=True)
        (root / job_id / "job.json").write_text(
            Job(job_id, "t", job_id, ("x",), JobStatus.RUNNING, "2026-11-12T09:00:00+00:00", pid=pid).to_json()
        )
    restarted = JobRunner(root, cwd=tmp_path, env=dict(os.environ))
    assert restarted.get("20261112-090001-bbbb").status is JobStatus.LOST
    assert restarted.get("20261112-090000-aaaa").status is JobStatus.RUNNING
    restarted.stop("20261112-090000-aaaa")
    process.wait(timeout=10)
    assert _wait(restarted, "20261112-090000-aaaa").status is JobStatus.STOPPED  # it was asked to


def test_a_finished_job_can_be_deleted_with_its_files_and_a_running_one_cannot(runner):
    slow = runner.start("t", "Sleeps", _python("import time; time.sleep(30)"))
    with pytest.raises(RuntimeError):
        runner.delete(slow.id)
    runner.stop(slow.id)
    _wait(runner, slow.id)
    runner.delete(slow.id)
    assert not runner.folder(slow.id).exists() and runner.list() == []


def test_files_outside_a_jobs_inputs_and_outputs_are_refused(runner):
    job = runner.start(
        "t",
        "Writes",
        lambda folder: [sys.executable, "-c", f"open({str(folder / 'outputs' / 'a.txt')!r}, 'w').write('x')"],
    )
    _wait(runner, job.id)
    assert runner.files(job.id, "outputs") == [("a.txt", 1)]
    assert runner.file(job.id, "outputs", "a.txt").read_text() == "x"
    for kind, relative in (("outputs", "../job.json"), ("outputs", "../../x"), ("log", "a.txt"), ("outputs", "nope")):
        with pytest.raises(KeyError):
            runner.file(job.id, kind, relative)
    with pytest.raises(KeyError):
        runner.get("../etc")


def test_formats():
    now = datetime(2026, 11, 12, 18, 0, tzinfo=UTC).astimezone()
    assert formats.when(now.isoformat(), now) == f"{now:%H:%M}"
    assert formats.when((now - timedelta(days=1)).isoformat(), now) == f"yesterday {now - timedelta(days=1):%H:%M}"
    earlier = now - timedelta(days=3)
    assert formats.when(earlier.isoformat(), now) == f"{earlier:%-d %b %H:%M}"
    last_year = now - timedelta(days=400)
    assert formats.when(last_year.isoformat(), now) == f"{last_year:%-d %b %Y}"
    assert formats.clock(now) == f"{now:%H:%M:%S}"
    assert [formats.took(s) for s in (14, 480, 34500)] == ["14 s", "8 min", "9 h 35 min"]
    assert [formats.size(n) for n in (512, 2048, 5 * 1024**2)] == ["512 bytes", "2.0 KB", "5.0 MB"]
    assert (
        formats.shell([sys.executable, "-m", "cfr_v2.intake", "--seed", "a b"])
        == "python -m cfr_v2.intake --seed 'a b'"
    )


def test_a_form_starts_a_real_job_whose_log_streams_and_whose_output_downloads(tmp_path, monkeypatch):
    """The intake query: a real pipeline command that needs no database."""
    root = __import__("pathlib").Path(__file__).resolve().parents[3]
    settings = ConsoleSettings(
        repo_root=root, workspace=tmp_path, env=dict(os.environ) | {"CFR_RELEASE_ID": "cfr-v2-phase-e"}
    )
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    assert client.post("/commands/intake.sql", data={"by_case_no": "on"}).status_code == 403  # no token
    bad = client.post("/commands/intake.synthetic", data={"csrf": app.state.csrf, "count": "0", "seed": "1"})
    assert bad.status_code == 422 and "Cases: at least 1." in bad.text and 'value="0"' in bad.text

    started = client.post(
        "/commands/intake.sql", data={"csrf": app.state.csrf, "by_case_no": "on"}, follow_redirects=False
    )
    assert started.status_code == 303
    job_id = started.headers["location"].rsplit("/", 1)[-1]
    job = _wait(app.state.runner, job_id)
    assert job.status is JobStatus.DONE and job.summary == "intake.sql"

    with client.stream("GET", f"/jobs/{job_id}/log") as stream:
        body = "".join(stream.iter_text())
    assert "event: log" in body and "UNNEST(@case_nos)" in body and "event: end" in body
    page = client.get(f"/jobs/{job_id}").text
    assert "Intake query · by case number" in page and "python -m cfr_v2.intake --print-sql" in page
    assert client.get(f"/jobs/{job_id}/files/outputs/intake.sql").text.count("UNNEST(@case_nos)") == 1
    assert "UNNEST" in client.get(f"/jobs/{job_id}/log.txt").text
    assert client.get(f"/jobs/{job_id}/files/outputs/../job.json").status_code == 404
    assert job.title in client.get("/partials/journal").text and job.title in client.get("/jobs?status=done").text
    assert job.title not in client.get("/jobs?status=failed").text
    assert "Intake query" in client.get("/intake").text  # recent jobs under the area's commands

    # Run again: the form opens filled in from the job; a failed job says why, with the fix
    assert "Run again" in page and f'href="/intake?again={job_id}#intake.sql"' in page
    again = client.get(f"/intake?again={job_id}").text
    assert "Filled in from" in again and 'id="intake.sql" open' in again
    assert client.get(f"/routing?again={job_id}").status_code == 404  # another area's command
    failed = client.post(
        "/commands/procedures.search",
        data={"csrf": app.state.csrf, "query": "fees", "procedures": "sha256:" + "a" * 64, "workdir": str(tmp_path)},
        follow_redirects=False,
    )
    assert failed.status_code == 422 and "LightRAG store:" in failed.text  # refused in the form: no job


def test_stamps_carry_the_full_time():
    stamp = formats.stamp("2026-11-12T09:00:00+00:00")
    assert stamp.startswith("<time datetime=") and "2026" in stamp and stamp.endswith("</time>")


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        ("[1/35] a.pdf\n[12/35] Fees.pdf\n", (12, 35, None, 34)),
        (
            (
                "12:00:01 INFO     cfr_v2.procedures - lightrag: [480/1844] passages in the graph; 0 failed, 0 queued; "
                "batch 3 of 12 took 900s; about 2 h 10 min left\n"
            ),
            (480, 1844, "2 h 10 min", 26),
        ),
        ("starting\n", None),
        ("[0/0] nothing\n", None),
    ],
)
def test_progress_is_the_last_count_a_job_printed(tmp_path, log, expected):
    from cfr_v2.console.jobs import read_progress

    path = tmp_path / "log.txt"
    path.write_text(log)
    found = read_progress(path)
    assert (found and (found.done, found.total, found.left, found.percent)) == (expected or None)


def test_a_running_job_shows_its_progress_and_the_count_of_running_jobs(tmp_path):
    root = __import__("pathlib").Path(__file__).resolve().parents[3]
    app = create_app(ConsoleSettings(repo_root=root, workspace=tmp_path, env=dict(os.environ)))
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    runner = app.state.runner
    code = "import time; print('[3/10] Fees.pdf', flush=True); time.sleep(30)"
    job = runner.start("procedures.snapshot", "Snapshot · pdfs", _python(code))
    try:
        deadline = time.monotonic() + 10
        while "[3/10]" not in runner.log_path(job.id).read_text() and time.monotonic() < deadline:
            time.sleep(0.05)
        journal = client.get("/partials/journal").text
        assert '<progress max="10" value="3"' in journal and "3 of 10" in journal
        page = client.get(f"/jobs/{job.id}").text
        assert "<title>(1 running) Snapshot · pdfs · CFR console</title>" in page
        assert 'href="/jobs?status=running"' in page and "30%" in page and "every 5s" in page
        assert "Tell me when it ends" in page
    finally:
        runner.stop(job.id, force=True)
        _wait(runner, job.id)
    assert "running)" not in client.get("/").text
