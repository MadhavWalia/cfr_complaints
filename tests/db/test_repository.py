from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from cfr_v2.contracts import (
    AdmissionKey,
    AdmissionStatus,
    ArtifactMetadata,
    Classification,
    IngestionRequest,
    TaskResult,
    TaskStatus,
)
from cfr_v2.state import ArtifactConflictError, CaseStateRepository, CommitOutcome, case_run_id_for

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
REF = "sha256:" + "a" * 64
REF_B = "sha256:" + "b" * 64


def _request(case="C1", rev=0, **kw):
    return IngestionRequest(
        key=AdmissionKey(source_system="icare", source_case_id=case, ingestion_revision=rev),
        raw_transcript_ref=REF,
        received_at=NOW,
        **kw,
    )


def _admit(session_factory, request):
    with session_factory.begin() as s:
        return CaseStateRepository(s).admit(request, request_ref=REF, release_manifest_ref="rel@x")


def test_first_admission_creates_run_in_admitted_state(session_factory):
    rec = _admit(session_factory, _request())
    assert rec.status is AdmissionStatus.ACCEPTED
    assert rec.case_run_id == case_run_id_for(_request().key)
    with session_factory() as s:
        assert CaseStateRepository(s).state_history(rec.case_run_id) == ["ADMITTED"]


def test_duplicate_admission_recorded_without_new_run(session_factory):
    first = _admit(session_factory, _request())
    second = _admit(session_factory, _request())
    assert second.status is AdmissionStatus.DUPLICATE
    assert second.case_run_id == first.case_run_id
    assert second.reason is None
    with session_factory() as s:
        repo = CaseStateRepository(s)
        assert [a.status for a in repo.admissions_for(_request().key)] == [
            AdmissionStatus.ACCEPTED,
            AdmissionStatus.DUPLICATE,
        ]
        assert s.execute(sa.text("SELECT count(*) FROM case_run")).scalar() == 1


def test_duplicate_with_different_content_is_flagged(session_factory):
    _admit(session_factory, _request())
    dup = _admit(session_factory, _request(case_metadata={"prim_ctgy": "changed"}))
    assert dup.status is AdmissionStatus.DUPLICATE
    assert dup.reason == "content differs from accepted admission"


def test_new_ingestion_revision_creates_new_run_for_same_case(session_factory):
    a = _admit(session_factory, _request(rev=0))
    b = _admit(session_factory, _request(rev=1))
    assert a.case_id == b.case_id
    assert a.case_run_id != b.case_run_id
    assert b.status is AdmissionStatus.ACCEPTED


def test_concurrent_admissions_create_one_run(session_factory):
    results, errors = [], []

    def worker():
        try:
            results.append(_admit(session_factory, _request()))
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


def test_rejection_recorded_without_run(session_factory):
    with session_factory.begin() as s:
        rec = CaseStateRepository(s).reject(_request().key, reason="malformed", request_hash="0" * 64)
    assert rec.status is AdmissionStatus.REJECTED
    assert rec.case_run_id is None


def test_state_transitions_are_idempotent(session_factory):
    run = _admit(session_factory, _request()).case_run_id
    with session_factory.begin() as s:
        repo = CaseStateRepository(s)
        assert repo.record_state(run, "UNDERSTANDING") is True
        assert repo.record_state(run, "UNDERSTANDING") is False
        assert repo.current_state(run) == "UNDERSTANDING"


@pytest.mark.parametrize("statement", ["UPDATE case_run SET lane = 'realtime'", "DELETE FROM case_run_event"])
def test_tables_are_append_only(session_factory, statement):
    _admit(session_factory, _request())
    with pytest.raises(DBAPIError, match="append-only"):
        with session_factory.begin() as s:
            s.execute(sa.text(statement))


def test_truncate_rejected(session_factory):
    with pytest.raises(DBAPIError, match="append-only"):
        with session_factory.begin() as s:
            s.execute(sa.text("TRUNCATE task_attempt"))


def _commit(session_factory, run, refs=(REF,), task="c02.pass1"):
    with session_factory.begin() as s:
        return CaseStateRepository(s).commit_task_result(
            case_run_id=run,
            logical_task_id=task,
            plan_revision=0,
            program_version="v1",
            input_hash="1" * 64,
            result=TaskResult(task_id=task, status=TaskStatus.SUCCEEDED, output_artifact_refs=refs),
        )


def test_first_valid_commit_wins(session_factory):
    run = _admit(session_factory, _request()).case_run_id
    first = _commit(session_factory, run)
    same = _commit(session_factory, run)
    different = _commit(session_factory, run, refs=(REF_B,))
    assert first.outcome is CommitOutcome.COMMITTED
    assert same.outcome is CommitOutcome.DUPLICATE
    assert different.outcome is CommitOutcome.CONFLICT
    assert first.task_execution_id == same.task_execution_id == different.task_execution_id
    assert different.committed_output_hash == first.committed_output_hash
    with session_factory() as s:
        repo = CaseStateRepository(s)
        assert repo.task_attempts(run, "c02.pass1") == ["COMMITTED", "DUPLICATE", "CONFLICT"]
        assert repo.committed_tasks(run) == ["c02.pass1"]


def test_concurrent_identical_commits_store_one_result(session_factory):
    run = _admit(session_factory, _request()).case_run_id
    outcomes = []
    threads = [threading.Thread(target=lambda: outcomes.append(_commit(session_factory, run).outcome)) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert outcomes.count(CommitOutcome.COMMITTED) == 1
    assert outcomes.count(CommitOutcome.DUPLICATE) == 5


def test_artifact_registration_idempotent_and_conflict_checked(session_factory):
    meta = ArtifactMetadata(ref=REF, media_type="text/plain", classification=Classification.INTERNAL, size_bytes=3)
    with session_factory.begin() as s:
        repo = CaseStateRepository(s)
        repo.register_artifact(meta, bucket="b", key="k")
        repo.register_artifact(meta, bucket="b", key="k")
        assert repo.artifact_location(REF) == ("b", "k", Classification.INTERNAL)
    other = meta.model_copy(update={"classification": Classification.RESTRICTED})
    with pytest.raises(ArtifactConflictError):
        with session_factory.begin() as s:
            CaseStateRepository(s).register_artifact(other, bucket="b-restricted", key="k")
