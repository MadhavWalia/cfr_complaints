"""Procedures in the console (LDR-0043): add documents, append to a store in place, remove a store."""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cfr_v2.console import commands
from cfr_v2.console.app import create_app
from cfr_v2.console.commands import BY_ID, Context, FormError
from cfr_v2.console.jobs import Job, JobStatus
from cfr_v2.console.settings import ConsoleSettings
from cfr_v2.procedures.stores import store_name, write_marker

A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64


def _job(command, summary=None, *, status=JobStatus.DONE, argv=("x",), form=None, title="A job"):
    return Job(
        "20261125-110000-aaaa", command, title, argv, status, "2026-11-25T11:00:00+00:00", summary=summary, form=form
    )


@pytest.fixture
def root(tmp_path):
    (tmp_path / "registry" / "releases").mkdir(parents=True)
    (tmp_path / "registry" / "releases" / "cfr-v2-x.yaml").write_text(f"procedure_snapshot: '{A}'\n")
    store = tmp_path / store_name(A)
    store.mkdir()
    write_marker(store, {"procedures_ref": A, "state": "complete", "passages": 3, "lineage": [{"made": "build"}]})
    (store / "kv_store_doc_status.json").write_text("{}")
    return tmp_path


def _ctx(root, *, running=(), done=None, env=None):
    jobs = done or {"procedures.add": [_job("procedures.add", B)]}
    return Context(
        root=root,
        env=env or {"CFR_RELEASE_ID": "cfr-v2-x"},
        done_jobs=lambda command: jobs.get(command, []),
        running=lambda: list(running),
    )


def _append(ctx, **values):
    form = {"from_ref": A, "procedures": B, "count_only": "on", "remove_gone": "on", "concurrency": "16", **values}
    return BY_ID["procedures.append"].parse({k: v for k, v in form.items() if v is not None}, ctx)


def test_the_append_defaults_to_the_releases_store_and_the_newest_snapshot(root):
    ctx = _ctx(root)
    fields = {f.name: f for f in BY_ID["procedures.append"].fields}
    assert fields["procedures"].initial(ctx) == B  # the store's default is the release's pin (needs a full manifest)
    labels = dict(commands.known_snapshots(ctx))
    assert labels[A].endswith("store complete") and labels[B].startswith("bbbbbbbbbbbb… · added to")
    assert labels[B].endswith("no store")


def test_a_preview_runs_without_confirming_and_changes_nothing(root):
    values = _append(_ctx(root))
    argv = BY_ID["procedures.append"].args(values, root)
    assert argv[:4] == ["lightrag-append", B, "--from", A] and "--count-only" in argv
    assert "--in-place" not in argv and "--keep-gone" not in argv and "--backup" not in argv


def test_an_append_needs_the_confirmation_and_no_worker(root):
    with pytest.raises(FormError, match="Tick “I've read this”"):
        _append(_ctx(root), count_only=None)
    worker = _job("system.worker", status=JobStatus.RUNNING, title="Worker · at most 50 queued")
    with pytest.raises(FormError, match="Stop the worker under System"):
        _append(_ctx(root, running=[worker]), count_only=None, confirm="on")
    build = _job("procedures.build", status=JobStatus.RUNNING, title="LightRAG build")
    with pytest.raises(FormError, match="running against a store"):
        _append(_ctx(root, running=[build]), count_only=None, confirm="on")
    values = _append(_ctx(root), count_only=None, confirm="on", remove_gone=None, backup="on")
    argv = BY_ID["procedures.append"].args(values, root)
    assert {"--in-place", "--keep-gone", "--backup"} <= set(argv) and "--count-only" not in argv


def test_an_append_is_refused_for_the_wrong_store_or_a_taken_name(root):
    ctx = _ctx(root)
    with pytest.raises(FormError, match="that's the one the store holds"):
        _append(ctx, procedures=A)
    with pytest.raises(FormError, match="no store for sha256:bbb"):
        _append(ctx, from_ref=B, procedures=A)
    (root / store_name(B)).mkdir()
    with pytest.raises(FormError, match="already has a store"):
        _append(ctx)
    os.rmdir(root / store_name(B))
    write_marker(
        root / store_name(A), {"procedures_ref": A, "state": "appending", "appending_to": "sha256:" + "c" * 64}
    )
    with pytest.raises(FormError, match="part-way through an append"):
        _append(ctx)
    write_marker(root / store_name(A), {"procedures_ref": A, "state": "appending", "appending_to": B})
    assert _append(ctx)["procedures"] == B  # the same append: Run Again resumes it


def test_after_a_preview_the_next_step_is_the_append_with_the_box_still_to_tick(root):
    preview = _job(
        "procedures.append",
        "1 new · 0 gone · 0 changed · 2 unchanged",
        argv=("python", "-m", "cfr_v2.procedures", "lightrag-append", B, "--from", A, "--count-only"),
        form={"from_ref": A, "procedures": B, "count_only": "on", "confirm": "on", "remove_gone": "on"},
    )
    links = dict(BY_ID["procedures.append"].next_links(preview))
    assert list(links) == ["Append them"]
    assert "count_only" not in links["Append them"] and "confirm" not in links["Append them"]
    estimate = BY_ID["procedures.append"].estimate(_ctx(root, done={"procedures.append": [preview]}))
    assert estimate.calls == 3


def test_adding_documents_deletes_the_uploads_and_leaves_out_by_line(root):
    values = {"pdfs": [Path("inputs/New Procedure.pdf")], "previous": A, "leave_out": ("Other Procedure.pdf",)}
    argv = BY_ID["procedures.add"].args(values, root)
    assert argv[:3] == ["snapshot", "--add-to", A] and "inputs/New Procedure.pdf" in argv
    assert argv[argv.index("--leave-out") + 1] == "Other Procedure.pdf"
    assert "--undated" in argv and "--delete-after" in argv
    field = next(f for f in BY_ID["procedures.add"].fields if f.name == "leave_out")
    assert field.parse("Other Procedure.pdf\n Example, Aug'26.pdf \n", _ctx(root)) == (
        "Other Procedure.pdf",
        "Example, Aug'26.pdf",
    )


def test_only_unpinned_stores_can_be_removed_and_only_when_confirmed(root):
    loose = root / store_name(B)
    loose.mkdir()
    write_marker(loose, {"procedures_ref": B})
    ctx = _ctx(root)
    choices = dict(BY_ID["procedures.remove_store"].fields[0].options(ctx))
    assert list(choices) == [loose.name]  # the pinned one isn't offered
    with pytest.raises(FormError, match="for good"):
        BY_ID["procedures.remove_store"].parse({"folder": loose.name}, ctx)
    values = BY_ID["procedures.remove_store"].parse({"folder": loose.name, "confirm": "on"}, ctx)
    assert BY_ID["procedures.remove_store"].args(values, root) == ["remove-store", loose.name, "--yes"]


def test_the_procedures_page_shows_the_stores_and_the_warning(root, tmp_path_factory):
    env = dict(os.environ) | {"CFR_STATE_DATABASE_URL": "postgresql://x@127.0.0.1:1/x", "CFR_RELEASE_ID": "cfr-v2-x"}
    env.pop("CFR_LIGHTRAG_WORKDIR", None)
    app = create_app(ConsoleSettings(repo_root=root, workspace=tmp_path_factory.mktemp("ws"), env=env))
    page = TestClient(app, base_url="http://127.0.0.1:8765").get("/procedures").text
    assert 'id="stores-h">Stores</h2>' in page and store_name(A) in page and "cfr-v2-x" in page
    assert "This changes the store the current release uses." in page
    assert 'id="procedures.add"' in page and 'id="procedures.remove_store"' in page


def test_uploaded_pdfs_go_to_the_snapshot_command(root, tmp_path_factory, monkeypatch):
    env = dict(os.environ) | {"CFR_STATE_DATABASE_URL": "postgresql://x@127.0.0.1:1/x", "CFR_RELEASE_ID": "cfr-v2-x"}
    app = create_app(ConsoleSettings(repo_root=root, workspace=tmp_path_factory.mktemp("ws"), env=env))
    started = {}

    def start(command_id, title, argv, **kwargs):
        folder = tmp_path_factory.mktemp("job")
        (folder / "inputs").mkdir()
        started["argv"] = list(argv(folder))
        return _job(command_id)

    monkeypatch.setattr(app.state.runner, "start", start)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    page = client.get("/procedures").text
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    response = client.post(
        "/commands/procedures.add",
        data={"csrf": csrf, "previous": A, "leave_out": "Old Procedure.pdf"},
        files=[
            ("pdfs", ("One Procedure.pdf", BytesIO(b"%PDF-1.4 one"), "application/pdf")),
            ("pdfs", ("Two Procedure.pdf", BytesIO(b"%PDF-1.4 two"), "application/pdf")),
        ],
        follow_redirects=False,
    )
    assert response.status_code == 303
    argv = started["argv"]
    assert [Path(a).name for a in argv if a.endswith(".pdf") and "/" in a] == ["One Procedure.pdf", "Two Procedure.pdf"]
    assert argv[argv.index("--leave-out") + 1] == "Old Procedure.pdf"
