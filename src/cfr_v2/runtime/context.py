"""Process-wide runtime dependencies used by workflow steps.

DBOS workflows are module-level functions recovered by name after a crash, so
their dependencies cannot be closed over; they are looked up here instead.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from dbos import Queue, SQLAlchemyDatasource
from sqlalchemy.orm import Session, sessionmaker

from cfr_v2.artifacts import ArtifactStore
from cfr_v2.contracts import Lane

from .settings import RuntimeSettings
from .stages import StageHandler

R = TypeVar("R")

# Our transaction steps are idempotent inserts; READ COMMITTED avoids needless
# serialization retries under batch concurrency.
TX_ISOLATION = "READ COMMITTED"


@dataclass
class Runtime:
    settings: RuntimeSettings
    ds: SQLAlchemyDatasource
    store: ArtifactStore
    handlers: Mapping[str, StageHandler]
    sessions: sessionmaker[Session]  # for writes outside workflows (admission)
    queues: dict[Lane, Queue] = field(default_factory=dict)  # filled after launch

    def tx(self, name: str, fn: Callable[..., R], *args: Any) -> R:
        """Run ``fn`` as a DBOS transaction step: its writes and checkpoint commit together."""
        return self.ds.run_tx_step({"name": name, "isolation_level": TX_ISOLATION}, fn, *args)


_current: Runtime | None = None


def install(runtime: Runtime | None) -> None:
    global _current
    _current = runtime


def current() -> Runtime:
    if _current is None:
        raise RuntimeError("runtime not started; call cfr_v2.runtime.start_runtime()")
    return _current
