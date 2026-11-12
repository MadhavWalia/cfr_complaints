"""The console's shell (LDR-0023 step 1): localhost only, CSRF, the home page and its states."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import Depends
from fastapi.testclient import TestClient

from cfr_v2.console import status as pipeline
from cfr_v2.console.app import AREAS, create_app, require_csrf
from cfr_v2.console.settings import ConsoleSettings, is_secret, read_env_file
from cfr_v2.runtime.release_check import QueuedRelease


def _settings(tmp_path, **env):
    return ConsoleSettings(
        repo_root=tmp_path, workspace=tmp_path / "ws", env={"CFR_RELEASE_ID": "cfr-v2-phase-e", **env}
    )


def _client(tmp_path, status=None, monkeypatch=None):
    app = create_app(_settings(tmp_path))
    if monkeypatch is not None:
        monkeypatch.setattr(pipeline, "read_status", lambda db, resolve: status)
        app.state.db = SimpleNamespace(close=lambda: None)  # never a real connection
    return app, TestClient(app, base_url="http://127.0.0.1:8765")


def test_the_env_file_is_read_as_sourcing_it_would(tmp_path):
    (tmp_path / ".env").write_text(
        "# comment\n\nexport A=1\nB='two words'\nC=\"x#y\"\nD=plain # trailing\nnot a line\n", encoding="utf-8"
    )
    assert read_env_file(tmp_path / ".env") == {"A": "1", "B": "two words", "C": "x#y", "D": "plain"}
    assert read_env_file(tmp_path / "missing") == {}
    assert is_secret("GCP_SERVICE_ACCOUNT_JSON") and is_secret("S3_SECRET") and not is_secret("CFR_RELEASE_ID")


def test_settings_let_the_env_file_win_over_the_process(tmp_path, monkeypatch):
    monkeypatch.setenv("CFR_RELEASE_ID", "cfr-v2-phase-d")
    (tmp_path / ".env").write_text("CFR_RELEASE_ID=cfr-v2-phase-e\n", encoding="utf-8")
    settings = ConsoleSettings.load(tmp_path)
    assert settings.release_id == "cfr-v2-phase-e" and settings.env_file_keys == ("CFR_RELEASE_ID",)
    assert settings.workspace == tmp_path / "var" / "console"


def test_requests_for_another_host_are_refused(tmp_path):
    app = create_app(_settings(tmp_path))
    assert TestClient(app, base_url="http://evil.example").get("/healthz").status_code == 400
    assert TestClient(app, base_url="http://localhost:8765").get("/healthz").status_code == 200


def test_changes_need_the_pages_csrf_token(tmp_path):
    app, client = _client(tmp_path)

    @app.post("/test-change", dependencies=[Depends(require_csrf)])
    def change():
        return {"changed": True}

    assert client.post("/test-change").status_code == 403
    assert client.post("/test-change", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/test-change", headers={"X-CSRF-Token": app.state.csrf}).json() == {"changed": True}
    assert client.post("/test-change", data={"csrf": app.state.csrf}).status_code == 200
    assert app.state.csrf in client.get("/").text  # the page carries it


def _status(**kw):
    line = tuple(pipeline.Stop(s, label, n) for (s, label), n in zip(pipeline.LINE, (2, 3, 1, 0, 0, 48), strict=True))
    return pipeline.PipelineStatus(
        line=line, failed=kw.get("failed", 0), releases=kw.get("releases", ()), workers=kw.get("workers", ())
    )


def test_the_home_page_shows_the_line_queue_and_workers(tmp_path, monkeypatch):
    releases = (
        QueuedRelease("cfr-v2-phase-e@6ff69ff5259b", 3, 1, ()),
        QueuedRelease("cfr-v2-phase-d@a220ea87891f", 5, 0, (), unknown=True),
    )
    _, client = _client(tmp_path, _status(failed=7, releases=releases, workers=(("mac:123", 1),)), monkeypatch)
    page = client.get("/").text
    for text in (
        "C02 understanding",
        "Plan ready",
        ">48<",
        "cfr-v2-phase-e@6ff69ff5259b",
        "<span>3</span>",
        "matches this code",
        "never pinned",
        "mac:123",
        "is running 1 case",
        "7 case runs failed",
        "No jobs yet",
    ):
        assert text in page, text
    assert 'aria-current="page"' in page and "New cases use" in page


def test_an_unreachable_database_is_a_state_not_a_crash(tmp_path, monkeypatch):
    _, client = _client(tmp_path, pipeline.unavailable("OperationalError"), monkeypatch)
    partial = client.get("/partials/pipeline-status")
    assert partial.status_code == 200 and "Can't read the state database (OperationalError)" in partial.text


def test_every_area_has_a_page(tmp_path, monkeypatch):
    _, client = _client(tmp_path, _status(), monkeypatch)
    for path, label in AREAS:
        response = client.get(path)
        assert response.status_code == 200 and f"<h1>{label}</h1>" in response.text, path


def test_errors_are_pages_for_people_and_json_for_scripts(tmp_path):
    _, client = _client(tmp_path)
    page = client.get("/jobs/no-such-job", headers={"Accept": "text/html"})
    assert page.status_code == 404 and "<h1>Not found</h1>" in page.text and "No such job." in page.text
    expired = client.post("/commands/system.migrate", headers={"Accept": "text/html"})
    assert expired.status_code == 403 and "This page has expired" in expired.text and "Reload the page" in expired.text
    assert client.get("/jobs/no-such-job").json() == {"detail": "No such job."}
    partial = client.get("/jobs/no-such-job/head", headers={"Accept": "text/html", "HX-Request": "true"})
    assert partial.status_code == 404 and partial.json() == {"detail": "No such job."}


def test_the_system_page_shows_the_settings_in_use_without_secrets(tmp_path, monkeypatch):
    settings = ConsoleSettings(
        repo_root=tmp_path,
        workspace=tmp_path / "ws",
        env={
            "CFR_RELEASE_ID": "cfr-v2-phase-e",
            "CFR_GATEWAY_API_KEY": "sk-live-123",
            "CFR_STATE_DATABASE_URL": "postgresql://cfr:hunter2@db.example:5432/cfr_state",
        },
        env_file_keys=("CFR_GATEWAY_API_KEY", "CFR_RELEASE_ID", "CFR_STATE_DATABASE_URL"),
    )
    monkeypatch.setattr(pipeline, "read_status", lambda db, resolve: _status())
    app = create_app(settings)
    app.state.db = SimpleNamespace(close=lambda: None)
    page = TestClient(app, base_url="http://127.0.0.1:8765").get("/system").text
    assert "Settings in use" in page and "cfr-v2-phase-e" in page
    assert "sk-live-123" not in page and "hunter2" not in page
    assert "set, hidden" in page and "postgresql://cfr:***@db.example:5432/cfr_state" in page


def test_display_values_hide_secrets_and_url_passwords():
    from cfr_v2.console.settings import display_value

    assert display_value("CFR_S3_SECRET", "x") == "set, hidden" and display_value("CFR_S3_SECRET", "") == "empty"
    assert display_value("CFR_STATE_DATABASE_URL", "postgresql://u:p@h/db") == "postgresql://u:***@h/db"
    assert display_value("CFR_STATE_DATABASE_URL", "postgresql://h/db") == "postgresql://h/db"
    assert display_value("CFR_RELEASE_ID", "cfr-v2-phase-e") == "cfr-v2-phase-e"


def test_the_lines_counts_open_those_cases(tmp_path, monkeypatch):
    _, client = _client(tmp_path, _status(failed=7), monkeypatch)
    page = client.get("/").text
    assert 'href="/cases?state=PLAN_READY"' in page and 'href="/cases?state=FAILED"' in page


def test_a_failed_job_says_why_and_offers_the_fix(tmp_path):
    from dataclasses import replace

    from cfr_v2.console.jobs import Job, JobStatus

    app, client = _client(tmp_path)
    runner = app.state.runner
    job = Job(
        "20261112-123053-63f6",
        "procedures.build",
        "LightRAG build · lightrag_store",
        ("python", "-m", "cfr_v2.procedures"),
        JobStatus.FAILED,
        "2026-11-12T12:30:53+00:00",
        ended_at="2026-11-12T12:30:58+00:00",
        exit_code=1,
        reason="StoreMismatchError: lightrag_store was built from sha256:6a0c: build sha256:b724 in a new directory",
        form={"procedures": "sha256:b724", "workdir": "lightrag_store", "concurrency": "16"},
    )
    (runner.folder(job.id)).mkdir(parents=True)
    runner._save(job)
    runner.log_path(job.id).write_text("Traceback …\n")

    page = client.get(f"/jobs/{job.id}").text
    assert "Why it failed" in page and "StoreMismatchError: lightrag_store was built from" in page
    assert "a store holds only one" in page and 'href="/procedures#procedures.build">Build the store' in page
    assert f'href="/procedures?again={job.id}#procedures.build">Run again' in page
    journal = client.get("/partials/journal").text
    assert "StoreMismatchError: lightrag_store was built" in journal and "exit 1" not in journal
    again = client.get(f"/procedures?again={job.id}").text
    assert 'value="lightrag_store"' in again and "Filled in from" in again

    lost = replace(job, id="20261112-130000-aaaa", status=JobStatus.LOST, reason=None, form=None)
    runner.folder(lost.id).mkdir()
    runner._save(lost)
    lost_page = client.get(f"/jobs/{lost.id}").text
    assert "Outcome unknown" in lost_page and "Run again" not in lost_page
    assert "outcome unknown" in client.get("/partials/journal").text
