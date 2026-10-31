"""C01 end to end on the real stack: synthetic rows → admission → PLAN_READY.

Needs Postgres and RustFS from docker-compose. ``ee_utils`` and BigQuery are
replaced by the regex masker and in-memory rows (not installable here).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cfr_v2.contracts import (
    AuthorizationContext,
    CanonicalTranscript,
    Classification,
    IngestionRequest,
)
from cfr_v2.intake import PII_TOKEN, IntakeCoordinator, IntakeOutcome, ProfanityFilter
from cfr_v2.intake.fakes import (
    RegexMasker,
    synthetic_names,
    synthetic_row,
    synthetic_rows,
)
from cfr_v2.registry import DEFAULT_REGISTRY_DIR, ReleaseRegistry, load_pinned
from cfr_v2.runtime import FINAL_STATE
from cfr_v2.state import CaseStateRepository

from ..spike.conftest import wait_for_state

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[3]
READ_ALL = AuthorizationContext(
    principal="test", purpose="test", allowed_classifications=frozenset({"RESTRICTED", "INTERNAL"})
)


@pytest.fixture
def manifest():
    return ReleaseRegistry(REPO_ROOT / DEFAULT_REGISTRY_DIR).load("cfr-v2-phase-b")


@pytest.fixture
def coordinator(running, store, manifest):
    return IntakeCoordinator.pinned(
        manifest, store=store, masker=RegexMasker(synthetic_names()), profanity=ProfanityFilter(["frustration"])
    )


def _scalar(running, sql: str, **params):
    with running.db.transaction() as tx:
        return tx.scalar(sql, params)


def test_batch_admits_valid_rows_and_reports_the_rest(running, coordinator, manifest):
    rows = [
        *synthetic_rows(10, seed=1),
        synthetic_row(100, seed=1, msg_tx2=None),  # no transcript: admitted, flagged
        synthetic_row(101, seed=1, case_no=None),  # unidentifiable
        synthetic_row(102, seed=1, msg_tx2=f"bad {PII_TOKEN} text"),  # sentinel collision
    ]
    report = coordinator.admit_rows(rows)

    assert report.manifest_ref == manifest.ref
    assert report.summary() == {
        "ACCEPTED": 11,
        "DUPLICATE": 0,
        "REJECTED": 1,
        "UNIDENTIFIED": 1,
        "FAILED": 0,
        "NO_TRANSCRIPT": 1,
    }
    run_ids = [r.case_run_id for r in report.results if r.outcome is IntakeOutcome.ACCEPTED]
    wait_for_state(running.settings.state_database_url, run_ids, FINAL_STATE, timeout=60)

    # Every run is pinned to the manifest; the rejection is recorded without a run.
    assert _scalar(running, "SELECT count(DISTINCT release_manifest_ref) FROM case_run") == 1
    assert _scalar(running, "SELECT min(release_manifest_ref) FROM case_run") == manifest.ref
    rejected = next(r for r in report.results if r.outcome is IntakeOutcome.REJECTED)
    assert rejected.reason == "SENTINEL_COLLISION"
    assert _scalar(running, "SELECT reason FROM admission WHERE status = 'REJECTED'") == "SENTINEL_COLLISION"
    no_transcript = next(r for r in report.results if r.has_transcript is False)
    assert (
        _scalar(
            running,
            "SELECT detail->>'transcript' FROM case_run_event WHERE case_run_id = %(r)s AND state = 'ADMITTED'",
            r=no_transcript.case_run_id,
        )
        == "absent"
    )


def test_rerun_of_same_cases_is_all_duplicates(running, coordinator):
    rows = synthetic_rows(5, seed=2)
    first = coordinator.admit_rows(rows)
    wait_for_state(running.settings.state_database_url, [r.case_run_id for r in first.results], FINAL_STATE, timeout=60)

    second = coordinator.admit_rows(rows)  # new received_at, same content (OQ-23)
    assert second.count(IntakeOutcome.DUPLICATE) == 5
    assert all(r.reason is None for r in second.results)
    assert [r.case_run_id for r in second.results] == [r.case_run_id for r in first.results]
    assert _scalar(running, "SELECT count(*) FROM case_run") == 5
    with running.db.transaction() as s:
        assert CaseStateRepository(s).task_attempts(first.results[0].case_run_id, "c02.understanding") == ["COMMITTED"]


def test_models_only_ever_get_redacted_text_and_v1_fields(running, coordinator, store):
    row = synthetic_row(0, seed=3)
    result = coordinator.admit_case(row)
    wait_for_state(running.settings.state_database_url, [result.case_run_id], FINAL_STATE, timeout=30)

    request_ref = _scalar(running, "SELECT request_ref FROM case_run WHERE case_run_id = %(r)s", r=result.case_run_id)
    request = IngestionRequest.model_validate_json(
        store.get(request_ref, classification=Classification.RESTRICTED, authorization=READ_ALL)
    )
    assert set(request.case_metadata) == {"prim_ctgy", "prim_subctgy"}
    assert set(request.evaluation_labels) == {"latest_rslv_optn"}

    transcript = CanonicalTranscript.model_validate_json(
        store.get(request.transcript_ref, classification=Classification.INTERNAL, authorization=READ_ALL)
    )
    redacted = store.get(
        transcript.interactions[0].redacted_text_ref, classification=Classification.INTERNAL, authorization=READ_ALL
    ).decode()
    assert "@example.com" not in redacted
    assert not any(name in redacted for name in synthetic_names())

    # Every stored ref is registered with the classification it was written under.
    for ref, expected in [
        (request.source_record_ref, "RESTRICTED"),
        (request.transcript_ref, "INTERNAL"),
        (transcript.interactions[0].original_text_ref, "RESTRICTED"),
        (transcript.interactions[0].redacted_text_ref, "INTERNAL"),
        (transcript.interactions[0].redaction_map_ref, "RESTRICTED"),
    ]:
        assert (
            _scalar(running, "SELECT classification FROM artifact WHERE sha256 = %(h)s", h=ref.removeprefix("sha256:"))
            == expected
        )


def test_pinned_manifest_survives_registry_edits(running, store, tmp_path):
    registry_dir = tmp_path / "registry"
    shutil.copytree(REPO_ROOT / DEFAULT_REGISTRY_DIR, registry_dir)
    original = ReleaseRegistry(registry_dir).load("cfr-v2-phase-b")
    coordinator = IntakeCoordinator.pinned(original, store=store, masker=RegexMasker())

    path = registry_dir / "releases" / "cfr-v2-phase-b.yaml"
    path.write_text(path.read_text().replace("placeholder-0", "placeholder-1", 1))
    edited = ReleaseRegistry(registry_dir).load("cfr-v2-phase-b")
    assert edited.ref != original.ref

    pinned = load_pinned(coordinator.manifest_ref, store=store, db=running.db, authorization=READ_ALL)
    assert pinned == original


def test_transcript_with_nothing_to_redact_is_admitted(running, coordinator, store):
    """Redacted text identical to the original: same bytes, stored under both classifications."""
    row = synthetic_row(200, seed=3, msg_tx2="Customer asked about a late fee. Agent waived it.")
    (result,) = coordinator.admit_rows([row]).results
    assert result.outcome is IntakeOutcome.ACCEPTED, result.reason

    request_ref = _scalar(running, "SELECT request_ref FROM case_run WHERE case_run_id = %(r)s", r=result.case_run_id)
    request = IngestionRequest.model_validate_json(
        store.get(request_ref, classification=Classification.RESTRICTED, authorization=READ_ALL)
    )
    transcript = CanonicalTranscript.model_validate_json(
        store.get(request.transcript_ref, classification=Classification.INTERNAL, authorization=READ_ALL)
    )
    (interaction,) = transcript.interactions
    assert interaction.original_text_ref == interaction.redacted_text_ref
    for classification in Classification:
        assert store.get(interaction.redacted_text_ref, classification=classification, authorization=READ_ALL)
    sha = interaction.redacted_text_ref.removeprefix("sha256:")
    assert _scalar(running, "SELECT count(*) FROM artifact WHERE sha256 = %(h)s", h=sha) == 2
