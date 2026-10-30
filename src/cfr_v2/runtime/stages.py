"""Stage definitions for ``case_intake_workflow`` and their handlers.

Phase A ships placeholder handlers; each later phase swaps in the real
component (C02 Understanding, C03 Selector, ...). A handler is a plain function,
never aware of the runner, so it is unit-testable on its own.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    state: str  # coarse case-run state entered before the stage runs (HD-04)
    logical_task_id: str
    program_version: str


STAGES: tuple[Stage, ...] = (
    Stage("UNDERSTANDING", "c02.understanding", "placeholder-0"),
    Stage("SELECTING", "c03.primary_selection", "placeholder-0"),
    Stage("ROUTING", "c04.routing", "placeholder-0"),
    Stage("PLANNING", "c05.planning", "placeholder-0"),
)
FINAL_STATE = "PLAN_READY"


@dataclass(frozen=True)
class StageInput:
    case_run_id: str
    stage: Stage
    request_ref: str
    upstream_refs: tuple[str, ...]
    trace_id: str | None = None  # for TaskEnvelope.trace_id and gateway calls (LDR-0012)


@dataclass(frozen=True)
class StageOutput:
    content: bytes
    media_type: str = "application/json"


StageHandler = Callable[[StageInput], StageOutput]


def placeholder_handler(inp: StageInput) -> StageOutput:
    """Deterministic stand-in output, so retries produce identical artifacts."""
    body = {
        "placeholder": True,
        "stage": inp.stage.logical_task_id,
        "case_run_id": inp.case_run_id,
        "upstream": list(inp.upstream_refs),
    }
    return StageOutput(json.dumps(body, sort_keys=True).encode())


def default_handlers() -> Mapping[str, StageHandler]:
    return {stage.logical_task_id: placeholder_handler for stage in STAGES}
