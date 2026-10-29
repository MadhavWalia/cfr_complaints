from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_STATE_URL = "postgresql+psycopg://cfr:cfr@localhost:5432/cfr_state"
DEFAULT_DBOS_URL = "postgresql+psycopg://cfr:cfr@localhost:5432/cfr_dbos"


@dataclass(frozen=True)
class RuntimeSettings:
    state_database_url: str
    dbos_system_database_url: str
    app_name: str = "cfr-v2"
    # Pinned explicitly: DBOS only recovers workflows started by the same version.
    app_version: str = "0.1.0"
    executor_id: str = "local"
    batch_queue_concurrency: int = 50
    realtime_queue_concurrency: int = 10
    queue_polling_interval_sec: float = 1.0
    # Schema in the case-state DB holding DBOS transaction-step checkpoints.
    tx_checkpoint_schema: str = "dbos"
    # Extra connections beyond one per workflow slot (admission, recovery, polling).
    db_pool_headroom: int = 10
    # Replaces pool_pre_ping on hot pools: connections are recycled rather than pinged.
    db_pool_recycle_sec: int = 1800

    @property
    def workflow_slots(self) -> int:
        """Workflows this executor may run at once; every pool is sized from it."""
        return self.batch_queue_concurrency + self.realtime_queue_concurrency

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "RuntimeSettings":
        return cls(
            state_database_url=env.get("CFR_STATE_DATABASE_URL", DEFAULT_STATE_URL),
            dbos_system_database_url=env.get("CFR_DBOS_SYSTEM_DATABASE_URL", DEFAULT_DBOS_URL),
            app_version=env.get("CFR_APP_VERSION", cls.app_version),
            executor_id=env.get("CFR_EXECUTOR_ID", cls.executor_id),
            batch_queue_concurrency=int(env.get("CFR_BATCH_QUEUE_CONCURRENCY", cls.batch_queue_concurrency)),
            realtime_queue_concurrency=int(env.get("CFR_REALTIME_QUEUE_CONCURRENCY", cls.realtime_queue_concurrency)),
            queue_polling_interval_sec=float(env.get("CFR_QUEUE_POLLING_INTERVAL_SEC", cls.queue_polling_interval_sec)),
        )
