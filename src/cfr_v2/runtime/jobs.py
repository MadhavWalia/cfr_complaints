"""SQL for ``workflow_job`` and ``step_checkpoint`` (migration 0003, LDR-0011).

Claiming uses ``FOR UPDATE SKIP LOCKED``, so any number of processes can share a
lane without double-claiming. A job whose lease expired (its worker died) is
claimable again by anyone; ``attempts`` counts every claim.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from cfr_v2.contracts import Lane
from cfr_v2.state import Tx


@dataclass(frozen=True)
class ClaimedJob:
    case_run_id: str
    lane: Lane
    attempt: int
    max_attempts: int


def enqueue(tx: Tx, case_run_id: str, lane: Lane, *, max_attempts: int) -> bool:
    """Create the job for a run; a second call for the same run is a no-op."""
    return (
        tx.scalar(
            "INSERT INTO workflow_job (case_run_id, lane, max_attempts) VALUES (%(run)s, %(lane)s, %(max)s)"
            " ON CONFLICT (case_run_id) DO NOTHING RETURNING case_run_id",
            {"run": case_run_id, "lane": lane.value, "max": max_attempts},
        )
        is not None
    )


def claim(tx: Tx, lane: Lane, *, owner: str, limit: int, lease_sec: float) -> list[ClaimedJob]:
    rows = tx.all(
        "WITH picked AS ("
        "  SELECT case_run_id FROM workflow_job"
        "  WHERE lane = %(lane)s AND ("
        "    (status = 'QUEUED' AND run_after <= now()) OR (status = 'RUNNING' AND lease_expires_at < now()))"
        "  ORDER BY run_after, case_run_id LIMIT %(limit)s FOR UPDATE SKIP LOCKED)"
        " UPDATE workflow_job j SET status = 'RUNNING', attempts = j.attempts + 1, lease_owner = %(owner)s,"
        "  lease_expires_at = now() + make_interval(secs => %(lease)s), updated_at = now()"
        " FROM picked WHERE j.case_run_id = picked.case_run_id"
        " RETURNING j.case_run_id, j.attempts, j.max_attempts",
        {"lane": lane.value, "limit": limit, "owner": owner, "lease": lease_sec},
    )
    return [ClaimedJob(r[0], lane, r[1], r[2]) for r in rows]


def renew(tx: Tx, case_run_ids: list[str], *, owner: str, lease_sec: float) -> int:
    if not case_run_ids:
        return 0
    return tx.execute(
        "UPDATE workflow_job SET lease_expires_at = now() + make_interval(secs => %(lease)s), updated_at = now()"
        " WHERE case_run_id = ANY(%(ids)s) AND lease_owner = %(owner)s AND status = 'RUNNING'",
        {"ids": list(case_run_ids), "owner": owner, "lease": lease_sec},
    )


def finish(tx: Tx, case_run_id: str, *, status: str, error: str | None = None) -> None:
    """Mark the job done. Not fenced on the lease: the results it reports are committed.

    ``last_error`` keeps the error of an earlier attempt when a retry succeeded.
    """
    tx.execute(
        "UPDATE workflow_job SET status = %(status)s, lease_owner = NULL, lease_expires_at = NULL,"
        " last_error = COALESCE(%(error)s, last_error), updated_at = now() WHERE case_run_id = %(run)s",
        {"run": case_run_id, "status": status, "error": error},
    )


def retry_later(tx: Tx, case_run_id: str, *, owner: str, delay_sec: float, error: str) -> bool:
    """Requeue after a delay; False if this worker no longer holds the lease."""
    return (
        tx.execute(
            "UPDATE workflow_job SET status = 'QUEUED', lease_owner = NULL, lease_expires_at = NULL,"
            " run_after = now() + make_interval(secs => %(delay)s), last_error = %(error)s, updated_at = now()"
            " WHERE case_run_id = %(run)s AND lease_owner = %(owner)s AND status = 'RUNNING'",
            {"run": case_run_id, "owner": owner, "delay": delay_sec, "error": error},
        )
        == 1
    )


def release(tx: Tx, case_run_ids: list[str], *, owner: str) -> int:
    """Hand unfinished jobs back at shutdown so another worker can take them at once."""
    if not case_run_ids:
        return 0
    return tx.execute(
        "UPDATE workflow_job SET status = 'QUEUED', lease_owner = NULL, lease_expires_at = NULL, updated_at = now()"
        " WHERE case_run_id = ANY(%(ids)s) AND lease_owner = %(owner)s AND status = 'RUNNING'",
        {"ids": list(case_run_ids), "owner": owner},
    )


def job_status(tx: Tx, case_run_id: str) -> tuple[str, str | None] | None:
    row = tx.one("SELECT status, last_error FROM workflow_job WHERE case_run_id = %(run)s", {"run": case_run_id})
    return None if row is None else (row[0], row[1])


def checkpoints(tx: Tx, case_run_id: str) -> dict[str, Any]:
    return dict(
        tx.all("SELECT step_name, output FROM step_checkpoint WHERE case_run_id = %(run)s", {"run": case_run_id})
    )


def save_checkpoint(tx: Tx, case_run_id: str, step_name: str, output: dict[str, Any]) -> dict[str, Any]:
    """Store a step output; if one is already stored (a racing worker), return that one."""
    inserted = tx.scalar(
        "INSERT INTO step_checkpoint (case_run_id, step_name, output) VALUES (%(run)s, %(step)s, %(out)s::jsonb)"
        " ON CONFLICT (case_run_id, step_name) DO NOTHING RETURNING output",
        {"run": case_run_id, "step": step_name, "out": json.dumps(output, sort_keys=True)},
    )
    if inserted is not None:
        return inserted
    return tx.scalar(
        "SELECT output FROM step_checkpoint WHERE case_run_id = %(run)s AND step_name = %(step)s",
        {"run": case_run_id, "step": step_name},
    )
