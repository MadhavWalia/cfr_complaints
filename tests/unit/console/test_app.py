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
