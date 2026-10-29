from __future__ import annotations

import json

import pytest

from cfr_v2.contracts import ExecutionBudget, TaskStatus
from cfr_v2.llm import (
    BudgetExhaustedError,
    BudgetTracker,
    FakeModelClient,
    GuardedModelClient,
    ModelRequest,
    PermanentModelError,
    RetryableModelError,
    UnredactedPayloadError,
)


def _req(text="customer says card declined", redacted=True, program="c02.pass1"):
    return ModelRequest(
        program_id=program,
        program_version="v1",
        case_id="case",
        task_id="task",
        instructions="Extract allegations.",
        inputs={"transcript": text},
        redacted=redacted,
    )


def test_input_hash_is_deterministic_and_content_sensitive():
    assert _req().input_hash == _req().input_hash
    assert _req().input_hash != _req("other").input_hash


async def test_fixture_lookup_and_request_recording():
    fake = FakeModelClient({("c02.pass1", _req().input_hash): {"allegations": []}})
    resp = await fake.complete(_req())
    assert resp.output == {"allegations": []}
    assert resp.usage.input_tokens > 0
    assert fake.requests == [_req()]


async def test_responder_fallback_and_missing_fixture():
    fake = FakeModelClient(responder=lambda r: {"echo": r.inputs["transcript"]})
    assert (await fake.complete(_req("x"))).output == {"echo": "x"}
    with pytest.raises(LookupError):
        await FakeModelClient().complete(_req())


@pytest.mark.parametrize(
    "error, status", [(RetryableModelError, TaskStatus.FAILED_RETRYABLE), (PermanentModelError, TaskStatus.FAILED_PERMANENT)]
)
async def test_fixture_can_raise_errors(error, status):
    fake = FakeModelClient({("c02.pass1", _req().input_hash): error})
    with pytest.raises(error) as exc_info:
        await fake.complete(_req())
    assert exc_info.value.task_status is status


async def test_from_file(tmp_path):
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps([{"program_id": "c02.pass1", "input_hash": _req().input_hash, "output": {"ok": 1}}]))
    assert (await FakeModelClient.from_file(path).complete(_req())).output == {"ok": 1}


async def test_guard_rejects_unredacted_payload():
    guarded = GuardedModelClient(FakeModelClient(responder=lambda r: {}), BudgetTracker(ExecutionBudget()))
    with pytest.raises(UnredactedPayloadError):
        await guarded.complete(_req(redacted=False))
    assert guarded.tracker.model_calls == 0


async def test_guard_enforces_call_budget():
    tracker = BudgetTracker(ExecutionBudget(max_model_calls=2))
    guarded = GuardedModelClient(FakeModelClient(responder=lambda r: {}), tracker)
    await guarded.complete(_req())
    await guarded.complete(_req())
    with pytest.raises(BudgetExhaustedError):
        await guarded.complete(_req())
    assert tracker.model_calls == 2


async def test_guard_enforces_token_budget():
    tracker = BudgetTracker(ExecutionBudget(max_input_tokens=5))
    guarded = GuardedModelClient(FakeModelClient(responder=lambda r: {}), tracker)
    with pytest.raises(BudgetExhaustedError, match="input"):
        await guarded.complete(_req())
    assert tracker.input_tokens > 5
