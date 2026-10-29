from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_STATE_URL = "postgresql://cfr:cfr@localhost:5432/cfr_state"


def _default_executor_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class RuntimeSettings:
    state_database_url: str
    # Lease owner name; only needs to be unique per live process (LDR-0011).
    executor_id: str = field(default_factory=_default_executor_id)
    batch_queue_concurrency: int = 50
    realtime_queue_concurrency: int = 10
    queue_polling_interval_sec: float = 1.0
    # A worker that stops renewing for this long is presumed dead; its jobs are reclaimed.
    lease_sec: float = 30.0
    heartbeat_sec: float = 10.0
    # Whole-run attempts (crash recoveries and infrastructure retries) before FAILED.
    max_job_attempts: int = 5
    retry_backoff_sec: float = 2.0
    # Attempts of one produce step (model call + upload) inside a run attempt.
    step_max_attempts: int = 3
    step_backoff_sec: float = 1.0
    # Extra connections beyond one per workflow slot (polling, heartbeats, admission).
    db_pool_headroom: int = 10
    db_pool_recycle_sec: int = 1800

    @property
    def workflow_slots(self) -> int:
        """Workflows this executor may run at once; the connection pool is sized from it."""
        return self.batch_queue_concurrency + self.realtime_queue_concurrency

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "RuntimeSettings":
        defaults = cls(state_database_url=DEFAULT_STATE_URL)
        return cls(
            state_database_url=env.get("CFR_STATE_DATABASE_URL", DEFAULT_STATE_URL),
            executor_id=env.get("CFR_EXECUTOR_ID") or defaults.executor_id,
            batch_queue_concurrency=int(env.get("CFR_BATCH_QUEUE_CONCURRENCY", defaults.batch_queue_concurrency)),
            realtime_queue_concurrency=int(env.get("CFR_REALTIME_QUEUE_CONCURRENCY", defaults.realtime_queue_concurrency)),
            queue_polling_interval_sec=float(env.get("CFR_QUEUE_POLLING_INTERVAL_SEC", defaults.queue_polling_interval_sec)),
            lease_sec=float(env.get("CFR_LEASE_SEC", defaults.lease_sec)),
            heartbeat_sec=float(env.get("CFR_HEARTBEAT_SEC", defaults.heartbeat_sec)),
            max_job_attempts=int(env.get("CFR_MAX_JOB_ATTEMPTS", defaults.max_job_attempts)),
        )
