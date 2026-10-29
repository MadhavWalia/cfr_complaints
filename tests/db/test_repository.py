from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from cfr_v2.contracts import (
    AdmissionKey,
    AdmissionStatus,
    ArtifactMetadata,
    Classification,
    IngestionRequest,
    ReleaseManifest,
    TaskResult,
    TaskStatus,
)
from cfr_v2.state import (
    ArtifactConflictError,
    CaseStateRepository,
    CommitOutcome,
    ManifestConflictError,
    case_run_id_for,
)

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
REF = "sha256:" + "a" * 64
REF_B = "sha256:" + "b" * 64


def _request(case="C1", rev=0, **kw):
    return IngestionRequest(
        key=AdmissionKey(source_system="icare", source_case_id=case, ingestion_revision=rev),
        transcript_ref=REF,
        received_at=NOW,
        **kw,
    )


def _admit(db, request):
    with db.transaction() as s:
        return CaseStateRepository(s).admit(request, request_ref=REF, release_manifest_ref="rel@x")


def test_first_admission_creates_run_in_admitted_state(db):
    rec = _admit(db, _request())
    assert rec.status is AdmissionStatus.ACCEPTED
    assert rec.case_run_id == case_run_id_for(_request().key)
    with db.transaction() as s:
        assert CaseStateRepository(s).state_history(rec.case_run_id) == ["ADMITTED"]


def test_duplicate_admission_recorded_without_new_run(db):
    first = _admit(db, _request())
    second = _admit(db, _request())
    assert second.status is AdmissionStatus.DUPLICATE
    assert second.case_run_id == first.case_run_id
    assert second.reason is None
    with db.transaction() as s:
        repo = CaseStateRepository(s)
        assert [a.status for a in repo.admissions_for(_request().key)] == [
            AdmissionStatus.ACCEPTED,
            AdmissionStatus.DUPLICATE,
        ]
        assert s.scalar("SELECT count(*) FROM case_run") == 1


def test_duplicate_with_different_content_is_flagged(db):
    _admit(db, _request())
    dup = _admit(db, _request(case_metadata={"prim_ctgy": "changed"}))
    assert dup.status is AdmissionStatus.DUPLICATE
    assert dup.reason == "content differs from accepted admission"


def test_new_ingestion_revision_creates_new_run_for_same_case(db):
    a = _admit(db, _request(rev=0))
    b = _admit(db, _request(rev=1))
    assert a.case_id == b.case_id
    assert a.case_run_id != b.case_run_id
    assert b.status is AdmissionStatus.ACCEPTED


def test_concurrent_admissions_create_one_run(db):
    results, errors = [], []

    def worker():
        try:
            results.append(_admit(db, _request()))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert sorted(r.status for r in results).count(AdmissionStatus.ACCEPTED) == 1
    assert len({r.case_run_id for r in results}) == 1


def test_rejection_recorded_without_run(db):
    with db.transaction() as s:
        rec = CaseStateRepository(s).reject(_request().key, reason="malformed", request_hash="0" * 64)
    assert rec.status is AdmissionStatus.REJECTED
    assert rec.case_run_id is None


def test_state_transitions_are_idempotent(db):
    run = _admit(db, _request()).case_run_id
    with db.transaction() as s:
        repo = CaseStateRepository(s)
        assert repo.record_state(run, "UNDERSTANDING") is True
        assert repo.record_state(run, "UNDERSTANDING") is False
        assert repo.current_state(run) == "UNDERSTANDING"


@pytest.mark.parametrize("statement", ["UPDATE case_run SET lane = 'realtime'", "DELETE FROM case_run_event"])
def test_tables_are_append_only(db, statement):
    _admit(db, _request())
    with pytest.raises(db.driver.Error, match="append-only"), db.transaction() as s:
        s.execute(statement)


def test_truncate_rejected(db):
    with pytest.raises(db.driver.Error, match="append-only"), db.transaction() as s:
        s.execute("TRUNCATE task_attempt")


def _commit(db, run, refs=(REF,), task="c02.pass1"):
    with db.transaction() as s:
        return CaseStateRepository(s).commit_task_result(
            case_run_id=run,
            logical_task_id=task,
            plan_revision=0,
            program_version="v1",
            input_hash="1" * 64,
            result=TaskResult(task_id=task, status=TaskStatus.SUCCEEDED, output_artifact_refs=refs),
        )


def test_first_valid_commit_wins(db):
    run = _admit(db, _request()).case_run_id
    first = _commit(db, run)
    same = _commit(db, run)
    different = _commit(db, run, refs=(REF_B,))
    assert first.outcome is CommitOutcome.COMMITTED
    assert same.outcome is CommitOutcome.DUPLICATE
    assert different.outcome is CommitOutcome.CONFLICT
    assert first.task_execution_id == same.task_execution_id == different.task_execution_id
    assert different.committed_output_hash == first.committed_output_hash
    with db.transaction() as s:
        repo = CaseStateRepository(s)
        assert repo.task_attempts(run, "c02.pass1") == ["COMMITTED", "DUPLICATE", "CONFLICT"]
        assert repo.committed_tasks(run) == ["c02.pass1"]


def test_concurrent_identical_commits_store_one_result(db):
    run = _admit(db, _request()).case_run_id
    outcomes = []
    threads = [threading.Thread(target=lambda: outcomes.append(_commit(db, run).outcome)) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert outcomes.count(CommitOutcome.COMMITTED) == 1
    assert outcomes.count(CommitOutcome.DUPLICATE) == 5


def test_artifact_registration_idempotent_and_conflict_checked(db):
    meta = ArtifactMetadata(ref=REF, media_type="text/plain", classification=Classification.INTERNAL, size_bytes=3)
    with db.transaction() as s:
        repo = CaseStateRepository(s)
        repo.register_artifact(meta, bucket="b", key="k")
        repo.register_artifact(meta, bucket="b", key="k")
        assert repo.artifact_location(REF) == ("b", "k", Classification.INTERNAL)
    other = meta.model_copy(update={"classification": Classification.RESTRICTED})
    with pytest.raises(ArtifactConflictError):
        with db.transaction() as s:
            CaseStateRepository(s).register_artifact(other, bucket="b-restricted", key="k")


# -- Phase B: content hash and release manifests ----------------------------------


def test_refetch_with_new_arrival_time_is_a_clean_duplicate(db):
    _admit(db, _request())
    later = IngestionRequest(**{**_request().model_dump(), "received_at": datetime(2026, 11, 2, tzinfo=timezone.utc)})
    dup = _admit(db, later)
    assert dup.status is AdmissionStatus.DUPLICATE
    assert dup.reason is None


def test_admitted_detail_recorded(db):
    with db.transaction() as s:
        rec = CaseStateRepository(s).admit(
            _request(), request_ref=REF, release_manifest_ref="rel@x", admitted_detail={"transcript": "absent"}
        )
    with db.transaction() as s:
        detail = s.scalar(
            "SELECT detail FROM case_run_event WHERE case_run_id = %(r)s AND state = 'ADMITTED'",
            {"r": rec.case_run_id},
        )
    assert detail == {"transcript": "absent"}


def test_manifest_registration_is_idempotent_and_detects_ref_collisions(db):
    manifest = ReleaseManifest.build(release_id="r1")
    with db.transaction() as s:
        CaseStateRepository(s).register_manifest(manifest, artifact_ref=REF)
    with db.transaction() as s:
        CaseStateRepository(s).register_manifest(manifest, artifact_ref=REF)
        assert CaseStateRepository(s).manifest_artifact_ref(manifest.ref) == REF
    forged = ReleaseManifest.model_construct(release_id="r1", checksum=manifest.checksum[:12] + "f" * 52)
    with pytest.raises(ManifestConflictError), db.transaction() as s:
        CaseStateRepository(s).register_manifest(forged, artifact_ref=REF_B)
