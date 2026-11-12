"""Cases and releases (LDR-0023 step 4) without a database: the INTERNAL-only reader, Grafana links,
the releases read from the registry, and the pages when Postgres is down."""

from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.console import cases
from cfr_v2.console.app import create_app
from cfr_v2.console.settings import ConsoleSettings, write_env_value
from cfr_v2.contracts import Classification
from cfr_v2.procedures import stores
from cfr_v2.registry import ReleaseRegistry

REPO = Path(__file__).resolve().parents[3]
AT = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


class FakeDb:
    """Answers the classification lookup only: which buckets hold a sha256."""

    def __init__(self, classifications: dict[str, set[str]]):
        self.classifications = classifications

    @contextmanager
    def transaction(self):
        db = self

        class Tx:
            def column(self, sql, params):
                assert sql == cases.CLASSIFICATIONS_SQL
                return sorted(db.classifications.get(params["sha"], set()))

        yield Tx()


def _run(*refs: str, traceparent: str | None = None) -> cases.CaseRun:
    row = cases.CaseRow("run-1", "SYN11", "cfr-v2-phase-e", "cfr-v2-phase-e@abc", "batch", "PLAN_READY", AT, AT)
    stage = cases.StageResult("c02.pass2", "pass2-1", "SUCCEEDED", None, refs, AT)
    job = cases.WorkflowJob("SUCCEEDED", 1, 3, None, None, cases.trace_id(traceparent), AT)
    return cases.CaseRun(row, (cases.Event("ADMITTED", AT, {}),), (stage,), job)


def test_only_internal_results_of_the_run_itself_are_read():
    store = InMemoryArtifactStore()
    internal = store.put(b'{"claims": []}', media_type="application/json", classification=Classification.INTERNAL)
    restricted = store.put(b"transcript", media_type="text/plain", classification=Classification.RESTRICTED)
    other = store.put(b"{}", media_type="application/json", classification=Classification.INTERNAL)
    db = FakeDb({
        internal.metadata.ref.removeprefix("sha256:"): {"INTERNAL"},
        restricted.metadata.ref.removeprefix("sha256:"): {"RESTRICTED"},
        other.metadata.ref.removeprefix("sha256:"): {"INTERNAL"},
    })  # fmt: skip
    run = _run(internal.metadata.ref, restricted.metadata.ref)
    assert cases.read_result(db, store, run, internal.metadata.ref) == b'{"claims": []}'
    for ref in (restricted.metadata.ref, other.metadata.ref, "../etc/passwd"):
        with pytest.raises(cases.NotShown):  # RESTRICTED, or not one of this run's results
            cases.read_result(db, store, run, ref)
    # the reader itself refuses RESTRICTED too, whatever the database says
    assert cases.READER.allowed_classifications == frozenset({"INTERNAL"})


def test_grafana_links_open_the_runs_trace_and_logs():
    run = _run(traceparent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
    assert (
        cases.trace_id("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01") == "0af7651916cd43dd8448eb211c80319c"
    )
    assert cases.trace_id(None) is None and cases.trace_id("garbage") is None
    trace = urlsplit(cases.trace_url("http://localhost:3000/", run))
    assert trace.path == "/explore"
    pane = json.loads(parse_qs(trace.query)["panes"][0])["a"]
    assert pane["datasource"] == "tempo" and pane["queries"][0]["query"] == "0af7651916cd43dd8448eb211c80319c"
    assert int(pane["range"]["from"]) < int(AT.timestamp() * 1000) < int(pane["range"]["to"])
    logs = json.loads(parse_qs(urlsplit(cases.logs_url("http://localhost:3000", run)).query)["panes"][0])["a"]
    assert logs["queries"][0]["expr"] == '{service_name=~"cfr.*"} | trace_id="0af7651916cd43dd8448eb211c80319c"'
    assert cases.trace_url("http://g", _run()) is None and cases.logs_url("http://g", _run()) is None


def test_releases_come_from_the_registry_and_the_database(tmp_path):
    releases, error = cases.read_releases(REPO / "registry", None)
    assert error is None
    phase_e = next(r for r in releases if r.release_id == "cfr-v2-phase-e")
    assert phase_e.manifest is not None and phase_e.mismatches == []  # it matches this code
    phase_b = next(r for r in releases if r.release_id == "cfr-v2-phase-b")
    assert phase_b.mismatches  # older stage versions

    (tmp_path / "releases").mkdir()
    (tmp_path / "releases" / "broken.yaml").write_text("release_id: [")
    rows = [("gone", "gone@111111111111", AT, 4), ("broken", "broken@222222222222", AT, 0)]
    found, error = cases.read_releases(tmp_path, lambda: rows)
    by_id = {r.release_id: r for r in found}
    assert by_id["broken"].problem == "broken.yaml: while parsing a flow node"
    assert by_id["gone"].manifest is None and by_id["gone"].runs == 4

    def down():
        raise ConnectionError("db.internal.example:5432 refused")

    found, error = cases.read_releases(tmp_path, down)
    assert error == "ConnectionError"  # the type, never the message


@pytest.fixture
def client(tmp_path):
    env = dict(os.environ) | {
        "CFR_RELEASE_ID": "cfr-v2-phase-e",
        "CFR_STATE_DATABASE_URL": "postgresql://x@127.0.0.1:1/x",
    }
    app = create_app(ConsoleSettings(repo_root=REPO, workspace=tmp_path, env=env))
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_the_pages_say_when_the_database_is_down(client):
    page = client.get("/cases?state=ROUTING&q=SYN")
    assert page.status_code == 200 and "Can't read the state database (OperationalError)" in page.text
    assert 'value="SYN"' in page.text and '<option value="ROUTING" selected>' in page.text
    releases = client.get("/releases")
    assert releases.status_code == 200 and "Can't read the state database" in releases.text
    assert 'id="cfr-v2-phase-e"' in releases.text and "New cases use it" in releases.text
    assert "Matches this code" in releases.text and "this code runs pass1-2" in releases.text  # an older release
    assert client.get("/cases/run-1").status_code == 503


# --- the case page, with results shaped like the real stages' -----------------------------------

PASS1 = {
    "allegations": [{"text": "Charged a late fee after paying on time", "kind": "FACT", "scope": "CARD_ACCOUNT"}],
    "requests": [{"text": "Refund the fee", "request_type": "REFUND"}],
    "agent_statements": [{"text": "Said the payment posted late", "kind": "EXPLANATION"}],
    "dropped": [{"item_list": "allegations", "index": 1, "reason": "NO_SPAN"}],
}
PASS2 = {
    "claims": [
        {
            "claim_id": "c1",
            "statement": "A late fee was charged although the payment was on time",
            "grounding_status": "GROUNDED",
            "account_resolution": "RESOLVED",
            "event_window_start": "2026-09-01",
            "event_window_end": "2026-09-30",
            "evidence_needs": [{"description": "Payment posting date", "source_type": "payments_and_balance"}],
        }
    ],
    "findings": [{"issue": "UNCOVERED_ALLEGATION", "ref": "a2"}],
}
C03 = {"status": "SELECTED", "claim_id": "c1", "rationale": "The fee is what the customer wants put right."}
C04 = {
    "claims": [
        {
            "claim_id": "c1",
            "outcome": "ROUTED",
            "candidates": [
                {
                    "vertical_id": "fees_and_interest",
                    "confidence": 0.82,
                    "source": "MODEL",
                    "rationale": "A disputed late fee.",
                    "procedures": [
                        {"file_name": "Fees.pdf", "section_title": "Late fees", "page_start": 4, "page_end": 5}
                    ],
                }
            ],
            "sections_seen": [{"file_name": "Fees.pdf"}],
        }
    ],
}
RESULTS = {
    "c02.pass1": PASS1,
    "c02.pass2": PASS2,
    "c03.primary_selection": C03,
    "c04.routing": C04,
    "c05.planning": {"placeholder": True},
}


def fake_case(traceparent: str | None = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01") -> cases.CaseRun:
    row = cases.CaseRow("run-1", "SYN11", "cfr-v2-phase-e", "cfr-v2-phase-e@abc", "batch", "PLAN_READY", AT, AT)
    stages = tuple(
        cases.StageResult(task, "v1", "SUCCEEDED", None, (f"sha256:{n:064x}",), AT) for n, task in enumerate(RESULTS)
    )
    events = tuple(
        cases.Event(s, AT, {"attempt": 1} if s == "ROUTING" else {})
        for s in ("ADMITTED", "UNDERSTANDING", "SELECTING", "ROUTING", "PLANNING", "PLAN_READY")
    )
    job = cases.WorkflowJob("SUCCEEDED", 1, 3, None, None, cases.trace_id(traceparent), AT)
    return cases.CaseRun(row, events, stages, job)


def serve_fake_case(monkeypatch, run: cases.CaseRun, withheld: str = "c02.pass2") -> None:
    """The case page's reads, answered from ``RESULTS``; ``withheld`` is refused as RESTRICTED."""
    by_ref = {s.refs[0]: s.task for s in run.stages}

    def read_result(db, store, case, ref):
        if by_ref[ref] == withheld:
            raise cases.NotShown(ref)
        return json.dumps(RESULTS[by_ref[ref]]).encode()

    monkeypatch.setattr(cases, "read_case", lambda db, case_run_id: run if case_run_id == run.row.case_run_id else None)
    monkeypatch.setattr(cases, "read_result", read_result)


def test_the_case_page_shows_each_stages_result(client, monkeypatch):
    client.app.state.store = InMemoryArtifactStore()  # no S3 settings here
    serve_fake_case(monkeypatch, fake_case(), withheld="none")
    page = client.get("/cases/run-1").text
    for text in (
        "Charged a late fee after paying on time",  # Pass 1
        "1 item dropped by the checks",
        "Payment posting date",  # Pass 2
        "Corrected by the checks: Uncovered allegation.",
        "The fee is what the customer wants put right.",  # C03, with the claim's statement
        "fees_and_interest",  # C04
        "82%",
        "Late fees",
        "A placeholder: this stage isn",  # C05
        "/explore?schemaVersion=1",  # Grafana
        "attempt: 1",  # an event's detail
    ):
        assert text in page, text
    assert page.count("A late fee was charged although the payment was on time") == 4  # Pass 2, its JSON, C03, C04


def test_a_withheld_result_says_why_and_a_run_without_a_trace_has_no_links(client, monkeypatch):
    client.app.state.store = InMemoryArtifactStore()  # no S3 settings here
    serve_fake_case(monkeypatch, fake_case(traceparent=None), withheld="c02.pass2")
    page = client.get("/cases/run-1").text
    assert "It isn&#39;t INTERNAL, so the console doesn&#39;t show it." in page
    assert "No trace was recorded" in page and "/explore?" not in page
    assert client.get("/cases/other").status_code == 404


def test_stage_statuses_use_the_one_vocabulary():
    def stage(status):
        return cases.StageResult("c02.pass1", "v", status, None, (), AT)

    assert [(stage(s).pill, stage(s).status_label) for s in ("SUCCEEDED", "FAILED_RETRYABLE", "CANCELLED")] == [
        ("done", "done"),
        ("failed", "failed, to retry"),
        ("waiting", "cancelled"),
    ]


# --- making a release current --------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    """A copy of the registry, with a .env and the phase-e snapshot's store, so .env can be written."""
    root = tmp_path / "repo"
    shutil.copytree(REPO / "registry", root / "registry")
    (root / ".env").write_text("# settings\nexport CFR_RELEASE_ID=cfr-v2-phase-b  # old\nCFR_OTHER=1\n")
    snapshot = ReleaseRegistry(root / "registry").load("cfr-v2-phase-e").procedure_snapshot
    store = root / stores.store_name(snapshot)
    store.mkdir()
    (store / stores.STORE_MARKER).write_text(json.dumps({"procedures_ref": snapshot}))
    return root


def test_a_ready_release_can_be_made_current(repo, monkeypatch):
    monkeypatch.delenv("CFR_LIGHTRAG_WORKDIR", raising=False)
    settings = ConsoleSettings.load(repo, workspace=repo / "var")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    page = client.get("/releases").text
    assert '<form method="post" action="/releases/cfr-v2-phase-e/current"' in page
    assert "Can't be made current: its stage versions don&#39;t match this code" in page  # phase-b
    assert 'action="/releases/cfr-v2-phase-b/current"' not in page

    assert client.post("/releases/cfr-v2-phase-e/current").status_code == 403  # no CSRF token
    sent = client.post("/releases/cfr-v2-phase-e/current", data={"csrf": app.state.csrf}, follow_redirects=False)
    assert sent.status_code == 303 and sent.headers["location"] == "/releases#cfr-v2-phase-e"
    assert (repo / ".env").read_text() == "# settings\nexport CFR_RELEASE_ID=cfr-v2-phase-e\nCFR_OTHER=1\n"
    assert settings.release_id == "cfr-v2-phase-e" and app.state.runner.env["CFR_RELEASE_ID"] == "cfr-v2-phase-e"
    assert "New cases use it" in client.get("/releases").text

    refused = client.post("/releases/cfr-v2-phase-b/current", data={"csrf": app.state.csrf})
    assert refused.status_code == 409 and "don't match this code" in refused.json()["detail"]
    assert client.post("/releases/nope/current", data={"csrf": app.state.csrf}).status_code == 404
    assert "CFR_RELEASE_ID=cfr-v2-phase-e" in (repo / ".env").read_text()  # unchanged by the refusals


def test_a_release_whose_snapshot_has_no_store_is_not_made_current(repo, monkeypatch):
    monkeypatch.delenv("CFR_LIGHTRAG_WORKDIR", raising=False)
    releases, _ = cases.read_releases(repo / "registry", None)
    phase_e = next(r for r in releases if r.release_id == "cfr-v2-phase-e")
    assert cases.blockers(phase_e, repo, {}) == []
    assert cases.blockers(phase_e, repo, {"CFR_LIGHTRAG_WORKDIR": "elsewhere"}) == [
        "its procedure snapshot has no LightRAG store here: build it under Procedures"
    ]


def test_the_env_file_keeps_every_other_line(tmp_path):
    path = tmp_path / ".env"
    write_env_value(path, "CFR_RELEASE_ID", "a")  # no file yet
    assert path.read_text() == "CFR_RELEASE_ID=a\n"
    path.write_text("A=1\n# CFR_RELEASE_ID=commented\nCFR_RELEASE_ID=b\nB='x y'\n")
    write_env_value(path, "CFR_RELEASE_ID", "c")
    assert path.read_text() == "A=1\n# CFR_RELEASE_ID=commented\nCFR_RELEASE_ID=c\nB='x y'\n"
    for bad in ("a b", "a\nB=1", "$(x)", ""):
        with pytest.raises(ValueError, match="not a plain value"):
            write_env_value(path, "CFR_RELEASE_ID", bad)


def test_a_new_release_is_written_from_the_releases_page(repo, monkeypatch):
    monkeypatch.delenv("CFR_LIGHTRAG_WORKDIR", raising=False)
    settings = ConsoleSettings.load(repo, workspace=repo / "var")
    app = create_app(settings)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    snapshot = "sha256:" + "b" * 64
    page = client.get(f"/releases?snapshot={snapshot}").text
    assert 'id="new-release"' in page and f'value="{snapshot}"' in page  # from a finished build's next step
    verticals = ReleaseRegistry(repo / "registry").load("cfr-v2-phase-e").routing_verticals
    form = {
        "csrf": app.state.csrf,
        "base": "cfr-v2-phase-e",
        "release_id": "cfr-v2-phase-f",
        "procedure_snapshot": "__other__",
        "procedure_snapshot_other": snapshot,
        "routing_verticals": verticals,
    }
    sent = client.post("/releases/new", data=form, follow_redirects=False)
    assert sent.status_code == 303 and sent.headers["location"].startswith("/releases?written=cfr-v2-phase-f&base=")
    assert ReleaseRegistry(repo / "registry").load("cfr-v2-phase-f").procedure_snapshot == snapshot
    shown = client.get(sent.headers["location"]).text
    assert 'Wrote <span class="mono">registry/releases/cfr-v2-phase-f.yaml</span>' in shown
    assert "procedure_snapshot" in shown and "git add registry/releases/cfr-v2-phase-f.yaml" in shown
    # its snapshot has no store here, so it can't be made current yet
    assert "Can&#39;t be made current" in shown or "Can't be made current" in shown

    again = client.post("/releases/new", data=form)
    assert again.status_code == 422 and "cfr-v2-phase-f already exists" in again.text
    assert f'value="{snapshot}"' in again.text  # the typed ref is kept
    assert client.post("/releases/new", data={k: v for k, v in form.items() if k != "csrf"}).status_code == 403


def test_a_next_step_opens_its_form_filled_in(repo, monkeypatch):
    settings = ConsoleSettings.load(repo, workspace=repo / "var")
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    snapshot = "sha256:" + "c" * 64
    page = client.get(f"/procedures?fill=procedures.build&procedures={snapshot}").text
    assert 'id="procedures.build" open' in page and f'value="{snapshot}"' in page
    assert 'value="16"' in page  # the other fields keep their defaults
    assert client.get("/procedures?fill=routing.trial").status_code == 404
