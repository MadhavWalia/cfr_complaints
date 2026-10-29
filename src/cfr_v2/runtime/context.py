"""Process-wide runtime dependencies.

Jobs can be claimed by any process sharing the database, so the code that runs
them looks its dependencies up here rather than closing over them.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cfr_v2.artifacts import ArtifactStore
from cfr_v2.state import Database

from .settings import RuntimeSettings
from .stages import StageHandler

if TYPE_CHECKING:
    from .runner import Runner


@dataclass
class Runtime:
    settings: RuntimeSettings
    db: Database
    store: ArtifactStore
    handlers: Mapping[str, StageHandler]
    runner: Runner | None = None  # set by start_runtime; None until the lanes start


_current: Runtime | None = None


def install(runtime: Runtime | None) -> None:
    global _current
    _current = runtime


def current() -> Runtime:
    if _current is None:
        raise RuntimeError("runtime not started; call cfr_v2.runtime.start_runtime()")
    return _current
