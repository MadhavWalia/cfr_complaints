"""The console's Cases and Releases pages on the real runner (LDR-0023 step 4): case runs reach the
final state with placeholder stages, and the console lists them, shows each one's timeline and
results, serves its INTERNAL results and refuses anything RESTRICTED."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cfr_v2.artifacts import register
from cfr_v2.console.app import create_app
from cfr_v2.console.settings import ConsoleSettings
from cfr_v2.contracts import Classification, ReleaseManifest
from cfr_v2.registry import pin_manifest
from cfr_v2.runtime import FINAL_STATE, submit
from cfr_v2.state import CaseStateRepository, Database

from .spike.conftest import make_request, run_id, wait_for_state

pytestmark = pytest.mark.db
REPO = Path(__file__).resolve().parents[2]


def test_the_console_shows_case_runs_and_only_their_internal_results(running, store, fresh_db, tmp_path):
    manifest_ref = pin_manifest(ReleaseManifest.build(release_id="cfr-v2-console-test"), store=store, db=running.db)
    for case in ("CONSOLE-1", "CONSOLE-2"):
        submit(make_request(case), release_manifest_ref=manifest_ref)
    wait_for_state(fresh_db[1], [run_id("CONSOLE-1"), run_id("CONSOLE-2")], FINAL_STATE, timeout=60)

    secret = store.put(b"a transcript", media_type="text/plain", classification=Classification.RESTRICTED)
    with running.db.transaction() as tx:
        register(CaseStateRepository(tx), secret)
        [pass1_ref] = tx.column(
            "SELECT output_artifact_refs->>0 FROM task_execution WHERE case_run_id = %(r)s AND logical_task_id = 'c02.pass1'",
            {"r": run_id("CONSOLE-1")},
        )

    env = dict(os.environ) | {"CFR_STATE_DATABASE_URL": fresh_db[1], "CFR_RELEASE_ID": "cfr-v2-console-test"}
    app = create_app(ConsoleSettings(repo_root=REPO, workspace=tmp_path, env=env))
    app.state.db, app.state.store = Database(fresh_db[1], max_connections=2), store
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    try:
        listed = client.get("/cases").text
        assert "CONSOLE-1" in listed and "CONSOLE-2" in listed and "Plan ready" in listed
        assert "CONSOLE-2" not in client.get("/cases?q=sole-1").text  # part of a case number, any case
        assert "CONSOLE-1" in client.get(f"/cases?q={run_id('CONSOLE-1')}").text
        assert "No case runs match" in client.get("/cases?state=FAILED").text
        assert "CONSOLE-1" in client.get("/cases?release=cfr-v2-console-test").text

        page = client.get(f"/cases/{run_id('CONSOLE-1')}").text
        for text in ('Case <span class="mono-h">CONSOLE-1</span>', "C02 understanding", "C05 planning", "Plan ready"):
            assert text in page
        assert "C02 Pass 1" in page and "C04 routing" in page and "A placeholder" in page

        result = client.get(f"/cases/{run_id('CONSOLE-1')}/results/{pass1_ref}")
        assert result.status_code == 200 and json.loads(result.content)["placeholder"] is True
        assert "attachment" in result.headers["content-disposition"]
        # RESTRICTED content and another run's results are never served
        assert client.get(f"/cases/{run_id('CONSOLE-1')}/results/{secret.metadata.ref}").status_code == 404
        assert client.get(f"/cases/{run_id('CONSOLE-2')}/results/{pass1_ref}").status_code == 404
        assert client.get("/cases/no-such-run").status_code == 404

        releases = client.get("/releases").text
        assert 'id="cfr-v2-console-test"' in releases and "no longer in the registry" in releases
        assert 'href="/cases?release=cfr-v2-console-test">2</a>' in releases
    finally:
        app.state.db.close()
