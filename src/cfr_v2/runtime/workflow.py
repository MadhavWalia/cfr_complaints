"""``case_intake_workflow``: run, or resume, one case run (LDR-0011).

Durability comes from what is already committed, not from replaying history:

- each stage's committed ``task_execution`` row is its checkpoint; a resumed run
  skips any stage whose result exists for the same input hash;
- a produce step's output (model call + S3 upload) is saved in
  ``step_checkpoint`` before its commit, so a crash in between does not repeat it;
- a stage's result, its next coarse state and, for the last stage, the job's
  completion commit in one transaction.

Stage handlers stay plain functions with no knowledge of the runner.
"""
from __future__ import annotations

import time
from typing import Any

from cfr_v2.artifacts import StoredArtifact, register
from cfr_v2.contracts import ArtifactMetadata, Classification, TaskResult, TaskStatus, canonical_hash
from cfr_v2.state import CaseStateRepository
from cfr_v2.telemetry import get_logger

from . import jobs
from .context import Runtime
from .stages import FINAL_STATE, STAGES, Stage, StageInput

logger = get_logger(__name__)

WORKFLOW_NAME = "case_intake_workflow"
PLAN_REVISION = 0  # fixed until C05 plan revisions (Phase F)


class StageFailedError(RuntimeError):
    """A stage's produce step failed on every attempt; the run fails permanently."""


def stage_input_hash(request_ref: str, logical_task_id: str, upstream: tuple[str, ...]) -> str:
    return canonical_hash({"request_ref": request_ref, "task": logical_task_id, "upstream": list(upstream)})


def run_job(rt: Runtime, job: jobs.ClaimedJob) -> str:
    """Execute a claimed job and settle it: SUCCEEDED, FAILED, or requeued (RETRY)."""
    run_id = job.case_run_id
    if job.attempt > job.max_attempts:
        _fail(rt, run_id, "MAX_ATTEMPTS_EXCEEDED")
        return "FAILED"
    try:
        _execute(rt, run_id)
        return "SUCCEEDED"
    except StageFailedError as exc:
        cause = type(exc.__cause__).__name__ if exc.__cause__ else "StageFailedError"
        logger.warning("workflow: %s failed permanently in %s (%s)", run_id, exc, cause)
        _fail(rt, run_id, cause)
        return "FAILED"
    except Exception as exc:  # infrastructure (DB, S3); the whole run is retried
        error = type(exc).__name__
        if job.attempt >= job.max_attempts:
            logger.exception("workflow: %s failed after %d attempts", run_id, job.attempt)
            _fail(rt, run_id, error)
            return "FAILED"
        delay = rt.settings.retry_backoff_sec * 2 ** (job.attempt - 1)
        logger.warning("workflow: %s attempt %d failed (%s); retrying in %.1fs", run_id, job.attempt, error, delay)
        with rt.db.transaction() as tx:
            jobs.retry_later(tx, run_id, owner=rt.settings.executor_id, delay_sec=delay, error=error)
        return "RETRY"


def _execute(rt: Runtime, run_id: str) -> None:
    with rt.db.transaction() as tx:
        repo = CaseStateRepository(tx)
        run = repo.case_run(run_id)
        if run is None:
            raise LookupError(f"case run {run_id} not found")
        committed = {
            (c.logical_task_id, c.input_hash, c.program_version, c.plan_revision): c
            for c in repo.committed_results(run_id)
        }
        saved = jobs.checkpoints(tx, run_id)
        repo.record_state(run_id, STAGES[0].state)

    request_ref = run["request_ref"]
    upstream: list[str] = []
    finished = False
    for i, stage in enumerate(STAGES):
        last = i == len(STAGES) - 1
        input_hash = stage_input_hash(request_ref, stage.logical_task_id, tuple(upstream))
        done = committed.get((stage.logical_task_id, input_hash, stage.program_version, PLAN_REVISION))
        if done is not None:
            upstream.append(done.output_artifact_refs[0])
            continue

        step = f"{stage.logical_task_id}.produce"
        stored = saved.get(step)
        if stored is None:
            stored = _produce(rt, run_id, stage, request_ref, tuple(upstream))
            with rt.db.transaction() as tx:
                stored = jobs.save_checkpoint(tx, run_id, step, stored)

        next_state = FINAL_STATE if last else STAGES[i + 1].state
        with rt.db.transaction() as tx:
            _commit(CaseStateRepository(tx), run_id, stage, input_hash, stored, next_state)
            if last:
                jobs.finish(tx, run_id, status="SUCCEEDED")
                finished = True
        upstream.append(stored["ref"])

    if not finished:  # every stage was already committed (resumed after the final commit)
        with rt.db.transaction() as tx:
            CaseStateRepository(tx).record_state(run_id, FINAL_STATE)
            jobs.finish(tx, run_id, status="SUCCEEDED")


def _produce(rt: Runtime, run_id: str, stage: Stage, request_ref: str, upstream: tuple[str, ...]) -> dict[str, Any]:
    settings = rt.settings
    for attempt in range(1, settings.step_max_attempts + 1):
        try:
            output = rt.handlers[stage.logical_task_id](StageInput(run_id, stage, request_ref, upstream))
            stored = rt.store.put(output.content, media_type=output.media_type, classification=Classification.INTERNAL)
        except Exception as exc:  # handler or upload; retried, then the run fails
            if attempt == settings.step_max_attempts:
                raise StageFailedError(stage.logical_task_id) from exc
            logger.warning("workflow: %s %s attempt %d failed (%s)", run_id, stage.logical_task_id, attempt, type(exc).__name__)
            time.sleep(settings.step_backoff_sec * 2 ** (attempt - 1))
            continue
        return {
            "ref": stored.metadata.ref,
            "bucket": stored.bucket,
            "key": stored.key,
            "media_type": stored.metadata.media_type,
            "classification": stored.metadata.classification.value,
            "size_bytes": stored.metadata.size_bytes,
        }
    raise AssertionError("unreachable")


def _commit(
    repo: CaseStateRepository, run_id: str, stage: Stage, input_hash: str, stored: dict[str, Any], next_state: str
) -> str:
    meta = ArtifactMetadata(
        ref=stored["ref"],
        media_type=stored["media_type"],
        classification=Classification(stored["classification"]),
        size_bytes=stored["size_bytes"],
    )
    register(repo, StoredArtifact(meta, stored["bucket"], stored["key"]))
    commit = repo.commit_task_result(
        case_run_id=run_id,
        logical_task_id=stage.logical_task_id,
        plan_revision=PLAN_REVISION,
        program_version=stage.program_version,
        input_hash=input_hash,
        result=TaskResult(task_id=stage.logical_task_id, status=TaskStatus.SUCCEEDED, output_artifact_refs=(meta.ref,)),
    )
    repo.record_state(run_id, next_state)
    return commit.outcome.value


def _fail(rt: Runtime, run_id: str, error: str) -> None:
    with rt.db.transaction() as tx:
        CaseStateRepository(tx).record_state(run_id, "FAILED", {"error": error})
        jobs.finish(tx, run_id, status="FAILED", error=error)
