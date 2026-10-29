"""``case_intake_workflow``: one durable DBOS workflow per case run (LDR-0002).

Determinism rules: the workflow body only orchestrates. All I/O happens in
steps — ``DBOS.run_step`` for S3/model work, ``Runtime.tx`` (DBOS transaction
steps) for case-state writes, which commit together with their checkpoint.
Inputs and step outputs are plain str/dict so checkpoints stay readable.
"""
from __future__ import annotations

from typing import Any

from dbos import DBOS

from cfr_v2.artifacts import StoredArtifact, register
from cfr_v2.contracts import ArtifactMetadata, Classification, TaskResult, TaskStatus, canonical_hash
from cfr_v2.state import CaseStateRepository
from cfr_v2.state import schema as t

from . import context
from .stages import FINAL_STATE, STAGES, StageInput

WORKFLOW_NAME = "case_intake_workflow"

PRODUCE_STEP_OPTIONS = {"retries_allowed": True, "max_attempts": 3, "interval_seconds": 1.0, "backoff_rate": 2.0}


@DBOS.workflow(name=WORKFLOW_NAME)
def case_intake_workflow(case_run_id: str) -> str:
    """Nine steps for four stages: each stage is one produce step plus one
    transaction that commits its result and enters the next coarse state."""
    rt = context.current()
    run = rt.tx("start", _start, case_run_id, STAGES[0].state)
    upstream: list[str] = []
    try:
        for i, stage in enumerate(STAGES):
            stored = DBOS.run_step(
                {"name": f"{stage.logical_task_id}.produce", **PRODUCE_STEP_OPTIONS},
                _produce,
                case_run_id,
                stage.logical_task_id,
                run["request_ref"],
                tuple(upstream),
            )
            next_state = STAGES[i + 1].state if i + 1 < len(STAGES) else FINAL_STATE
            rt.tx(
                f"{stage.logical_task_id}.commit",
                _commit,
                case_run_id,
                stage.logical_task_id,
                run["request_ref"],
                tuple(upstream),
                stored,
                next_state,
            )
            upstream.append(stored["ref"])
    except Exception as exc:
        rt.tx("failed", _enter_state, case_run_id, "FAILED", {"error": type(exc).__name__})
        raise
    return FINAL_STATE


# -- steps -------------------------------------------------------------------------


def _repo() -> CaseStateRepository:
    return CaseStateRepository(context.current().ds.sql_session())


def _start(case_run_id: str, first_state: str) -> dict[str, Any]:
    """Load the run and enter the first stage's state in one transaction."""
    repo = _repo()
    row = repo.session.execute(
        t.case_run.select().where(t.case_run.c.case_run_id == case_run_id)
    ).mappings().one()
    repo.record_state(case_run_id, first_state)
    return {"request_ref": row["request_ref"], "release_manifest_ref": row["release_manifest_ref"], "lane": row["lane"]}


def _enter_state(case_run_id: str, state: str, detail: dict | None = None) -> bool:
    return _repo().record_state(case_run_id, state, detail)


def _produce(case_run_id: str, logical_task_id: str, request_ref: str, upstream: tuple[str, ...]) -> dict[str, Any]:
    rt = context.current()
    stage = next(s for s in STAGES if s.logical_task_id == logical_task_id)
    output = rt.handlers[logical_task_id](StageInput(case_run_id, stage, request_ref, upstream))
    stored = rt.store.put(output.content, media_type=output.media_type, classification=Classification.INTERNAL)
    return {
        "ref": stored.metadata.ref,
        "bucket": stored.bucket,
        "key": stored.key,
        "media_type": stored.metadata.media_type,
        "classification": stored.metadata.classification.value,
        "size_bytes": stored.metadata.size_bytes,
    }


def stage_input_hash(request_ref: str, logical_task_id: str, upstream: tuple[str, ...]) -> str:
    return canonical_hash({"request_ref": request_ref, "task": logical_task_id, "upstream": list(upstream)})


def _commit(
    case_run_id: str,
    logical_task_id: str,
    request_ref: str,
    upstream: tuple[str, ...],
    stored: dict[str, Any],
    next_state: str,
) -> str:
    repo = _repo()
    stage = next(s for s in STAGES if s.logical_task_id == logical_task_id)
    meta = ArtifactMetadata(
        ref=stored["ref"],
        media_type=stored["media_type"],
        classification=Classification(stored["classification"]),
        size_bytes=stored["size_bytes"],
    )
    register(repo, StoredArtifact(meta, stored["bucket"], stored["key"]))
    commit = repo.commit_task_result(
        case_run_id=case_run_id,
        logical_task_id=logical_task_id,
        plan_revision=0,
        program_version=stage.program_version,
        input_hash=stage_input_hash(request_ref, logical_task_id, upstream),
        result=TaskResult(task_id=logical_task_id, status=TaskStatus.SUCCEEDED, output_artifact_refs=(meta.ref,)),
    )
    repo.record_state(case_run_id, next_state)
    return commit.outcome.value
