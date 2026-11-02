"""DSPy over the P5 model client (LDR-0015): identity, errors, isolation, no litellm."""

from __future__ import annotations

import asyncio
import sys

import dspy
import pytest

from cfr_v2.llm import (
    FakeModelClient,
    ModelOutputInvalidError,
    ModelRateLimitedError,
    PermanentModelError,
    UnredactedPayloadError,
)
from cfr_v2.llm.dspy_lm import ProgramCall, gateway_lm, run_program


class Echo(dspy.Signature):
    """Answer the question."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField()


def _call(case_id: str = "C1", **kw) -> ProgramCall:
    return ProgramCall(program_id="test.echo", program_version="1", case_id=case_id, task_id="t", redacted=True, **kw)


def _run(client, call=None, **inputs):
    lm = gateway_lm(client, "fake-model")
    return asyncio.run(run_program(dspy.Predict(Echo), lm=lm, call=call or _call(), **(inputs or {"question": "q?"})))


def test_each_lm_call_is_one_model_request_with_the_program_identity():
    client = FakeModelClient(responder=lambda r: {"answer": "42"})
    prediction = _run(client, call=_call(trace_id="abc"), question="what is six times seven?")
    assert prediction.answer == "42"
    [request] = client.requests
    assert (request.program_id, request.program_version, request.case_id, request.trace_id) == (
        "test.echo",
        "1",
        "C1",
        "abc",
    )
    assert request.redacted and request.inputs == {}
    assert "Answer the question." in request.instructions
    assert "what is six times seven?" not in request.instructions  # data stays out of the instructions (HD-08)
    assert request.messages[-1].role == "user" and "what is six times seven?" in request.messages[-1].content
    assert request.output_schema["required"] == ["answer"]  # DSPy's json_schema reaches the gateway


def test_gateway_errors_come_back_as_themselves():
    client = FakeModelClient(responder=lambda r: (_ for _ in ()).throw(ModelRateLimitedError("x", retry_after_sec=7)))
    with pytest.raises(ModelRateLimitedError) as caught:
        _run(client)
    assert caught.value.retry_after_sec == 7


def test_unparseable_output_is_invalid_output_without_the_reply_text():
    client = FakeModelClient(responder=lambda r: {"wrong_field": "SECRET-REPLY"})
    with pytest.raises(ModelOutputInvalidError) as caught:
        _run(client)
    assert "SECRET-REPLY" not in str(caught.value) and caught.value.__cause__ is None


def test_unredacted_call_is_refused_before_the_client():
    client = FakeModelClient(responder=lambda r: {"answer": "x"})
    with pytest.raises(UnredactedPayloadError):
        _run(client, call=ProgramCall("test.echo", "1", "C1", "t", redacted=False))
    assert client.requests == []


def test_call_outside_run_program_is_refused():
    client = FakeModelClient(responder=lambda r: {"answer": "x"})
    lm = gateway_lm(client, "fake-model")
    with dspy.context(lm=lm), pytest.raises(Exception) as caught:
        asyncio.run(dspy.Predict(Echo).acall(question="q"))
    assert isinstance(caught.value.__cause__, PermanentModelError)
    assert client.requests == []


def test_concurrent_programs_keep_their_own_case():
    client = FakeModelClient(responder=lambda r: {"answer": r.case_id})
    lm = gateway_lm(client, "fake-model")

    async def all_cases():
        return await asyncio.gather(
            *(run_program(dspy.Predict(Echo), lm=lm, call=_call(f"C{i}"), question=f"q{i}") for i in range(20))
        )

    answers = [p.answer for p in asyncio.run(all_cases())]
    assert answers == [f"C{i}" for i in range(20)]


def test_no_history_and_no_litellm():
    client = FakeModelClient(responder=lambda r: {"answer": "x"})
    _run(client)
    assert dspy.clients.base_lm.GLOBAL_HISTORY == []
    assert "litellm" not in sys.modules
