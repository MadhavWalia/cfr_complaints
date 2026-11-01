"""Gateway client (LDR-0013): classification, retries, rate limits, timeouts, structured output.

safechain is replaced by a scripted chat model; nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from cfr_v2.llm import (
    Failure,
    GatewayModelClient,
    GatewaySettings,
    ModelBlockedError,
    ModelConfigError,
    ModelLoop,
    ModelOutputInvalidError,
    ModelRateLimitedError,
    ModelRequest,
    ModelTimeoutError,
    PermanentModelError,
    RateGate,
    RetryableModelError,
    classify,
)

SECRET = "Jane Doe card 4111"  # must never surface in errors or logs
SCHEMA = {"title": "Allegations", "type": "object", "properties": {"allegations": {"type": "array"}}}

FAST = GatewaySettings(
    max_concurrency=4,
    max_attempts=3,
    backoff_base_sec=0.001,
    backoff_max_sec=0.002,
    attempt_timeout_sec=5.0,  # generous: a loaded CI host must not turn fake calls into timeouts
    call_deadline_sec=10.0,
    rate_limit_cooldown_sec=0.01,
    rate_limit_cooldown_max_sec=0.05,
)


class HTTPError(Exception):
    """Shaped like openai.APIStatusError: ``status_code`` and ``response.headers``."""

    def __init__(self, status: int, *, headers: dict | None = None, code: str | None = None) -> None:
        super().__init__(f"Error code: {status} - {SECRET}")
        self.status_code = status
        self.code = code
        self.response = SimpleNamespace(status_code=status, headers=headers or {})


class Script:
    """What successive calls return: a dict (JSON output), "invalid", "filtered", "refused",
    a delay in seconds, or an exception. Shaped like safechain's SafeAzureChatOpenAI."""

    def __init__(self, *steps, redaction: str = "disabled") -> None:
        self.steps = list(steps)
        self.redaction = redaction
        self.calls = 0
        self.builds: list[tuple[str, dict]] = []
        self.messages: list[list[dict]] = []
        self.kwargs: list[dict] = []
        self.inflight = self.peak = 0

    async def factory(self, key, params):
        self.builds.append((key, params))
        return FakeChat(self)

    async def next(self, messages, kwargs):
        self.calls += 1
        self.messages.append(messages)
        self.kwargs.append(kwargs)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(step if isinstance(step, float) else 0.005)
        finally:
            self.inflight -= 1
        if isinstance(step, BaseException):
            raise step
        return {"allegations": []} if isinstance(step, float) else step


class FakeChat:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.redaction = SimpleNamespace(mode=script.redaction)

    async def ainvoke(self, messages, **kwargs):
        out = await self.script.next(messages, kwargs)
        message = SimpleNamespace(
            content="hi",
            usage_metadata={"input_tokens": 120, "output_tokens": 30},
            response_metadata={"finish_reason": "stop"},
            additional_kwargs={},
        )
        if out == "invalid":
            message.content = '{"allegations": ['  # truncated
        elif out == "filtered":
            message.response_metadata = {"finish_reason": "content_filter"}
        elif out == "refused":
            message.additional_kwargs = {"refusal": SECRET}
        elif isinstance(out, dict) and "response_format" in kwargs:
            message.content = json.dumps(out)
        return message


def _req(schema=SCHEMA) -> ModelRequest:
    return ModelRequest(
        program_id="c02.pass1",
        program_version="v1",
        case_id="run-1",
        task_id="c02.understanding",
        instructions="List the customer's allegations.",
        inputs={"transcript": SECRET},
        redacted=True,
        output_schema=schema,
    )


def _client(script: Script, settings: GatewaySettings = FAST) -> GatewayModelClient:
    return GatewayModelClient(settings, model_factory=script.factory)


# --- happy path --------------------------------------------------------------------


async def test_structured_output_with_usage_and_separate_data_message():
    script = Script({"allegations": ["late fee"]})
    response = await _client(script).complete(_req())

    assert response.output == {"allegations": ["late fee"]}
    assert (response.usage.input_tokens, response.usage.output_tokens) == (120, 30)
    assert response.model_id == "gpt-5"
    system, user = script.messages[0]
    assert system == {"role": "system", "content": "List the customer's allegations."}
    assert SECRET not in system["content"]  # transcript only in the data message (HD-08)
    assert user["role"] == "user" and SECRET in user["content"]
    assert script.kwargs[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Allegations", "schema": SCHEMA, "strict": False},
    }


async def test_one_model_per_process_with_sdk_retries_off():
    script = Script({"allegations": []})
    client = _client(script)
    await asyncio.gather(*(client.complete(_req()) for _ in range(5)))
    assert len(script.builds) == 1  # safechain refreshes the token itself; no rebuild per call
    key, params = script.builds[0]
    assert key == "3"
    assert params == {
        "max_completion_tokens": 16384,
        "temperature": 0.0,
        "max_retries": 0,
        "request_timeout": FAST.attempt_timeout_sec,
    }


async def test_401_rebuilds_the_model():
    script = Script(HTTPError(401), {"allegations": []})
    await _client(script).complete(_req())
    assert len(script.builds) == 2


async def test_model_with_safechain_redaction_on_is_refused():
    script = Script({"allegations": []}, redaction="masking")
    with pytest.raises(ModelConfigError, match="redaction"):
        await _client(script).complete(_req())
    assert script.calls == 0


@pytest.mark.parametrize("step", ["filtered", "refused"])
async def test_blocked_or_refused_output_fails_permanently(step):
    script = Script(step)
    with pytest.raises(ModelBlockedError) as info:
        await _client(script).complete(_req())
    assert script.calls == 1
    assert SECRET not in str(info.value)


async def test_plain_text_when_no_schema():
    script = Script({"unused": 1})
    response = await _client(script).complete(_req(schema=None))
    assert response.output == {"text": "hi"}
    assert "response_format" not in script.kwargs[0]


def test_output_schema_changes_the_input_hash_only_when_set():
    assert _req(schema=None).input_hash != _req().input_hash
    plain = _req(schema=None).model_dump()
    plain.pop("output_schema")
    assert ModelRequest(**plain).input_hash == _req(schema=None).input_hash


# --- classification ------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, failure",
    [
        (HTTPError(429), Failure.RATE_LIMITED),
        (HTTPError(401), Failure.AUTH),
        (HTTPError(403), Failure.BLOCKED),
        (HTTPError(400, code="content_filter"), Failure.BLOCKED),
        (HTTPError(400), Failure.REJECTED),
        (HTTPError(404), Failure.REJECTED),
        (HTTPError(408), Failure.TRANSIENT),
        (HTTPError(500), Failure.TRANSIENT),
        (HTTPError(503), Failure.TRANSIENT),
        (TimeoutError(), Failure.TIMEOUT),
        (ConnectionResetError(), Failure.TRANSIENT),
        (type("APIConnectionError", (Exception,), {})(), Failure.TRANSIENT),
        (RuntimeError("429 Too Many Requests"), Failure.RATE_LIMITED),  # V1 message fallback
        (RuntimeError("Request blocked by Firewall"), Failure.BLOCKED),
        (RuntimeError("Could not contact endpoint"), Failure.TRANSIENT),
        (TypeError("bad arg"), Failure.BUG),
        (type("ConfigValidationError", (Exception,), {})("model 9 not defined"), Failure.BUG),
        (RuntimeError("something odd"), Failure.TRANSIENT),
    ],
)
def test_classify(exc, failure):
    assert classify(exc).failure is failure


def test_retry_after_header_is_read():
    assert classify(HTTPError(429, headers={"retry-after": "7"})).retry_after_sec == 7.0
    assert classify(HTTPError(429, headers={"retry-after-ms": "250"})).retry_after_sec == 0.25
    assert classify(HTTPError(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})).retry_after_sec is None


# --- retries -------------------------------------------------------------------------


async def test_transient_errors_retry():
    script = Script(HTTPError(503), type("APIConnectionError", (Exception,), {})(), {"allegations": []})
    await _client(script).complete(_req())
    assert script.calls == 3
    assert len(script.builds) == 1


async def test_transient_errors_exhaust_into_a_retryable_error():
    script = Script(HTTPError(502))
    with pytest.raises(RetryableModelError) as info:
        await _client(script).complete(_req())
    assert script.calls == FAST.max_attempts
    assert "HTTP 502" in str(info.value) and "transient" in str(info.value)


async def test_timeouts_exhaust_into_model_timeout():
    script = Script(1.0)  # every call takes longer than attempt_timeout_sec
    settings = GatewaySettings(**{**FAST.__dict__, "attempt_timeout_sec": 0.05})
    with pytest.raises(ModelTimeoutError):
        await _client(script, settings).complete(_req())
    assert script.calls == FAST.max_attempts


@pytest.mark.parametrize(
    "exc, error",
    [
        (HTTPError(403), ModelBlockedError),
        (HTTPError(400, code="content_filter"), ModelBlockedError),
        (HTTPError(422), PermanentModelError),
        (TypeError("bug"), PermanentModelError),
    ],
)
async def test_permanent_failures_are_not_retried(exc, error):
    script = Script(exc)
    with pytest.raises(error):
        await _client(script).complete(_req())
    assert script.calls == 1


async def test_rate_limits_cool_down_without_using_attempts():
    script = Script(*[HTTPError(429)] * 5, {"allegations": []})  # more 429s than max_attempts
    client = _client(script)
    started = time.monotonic()
    response = await client.complete(_req())
    assert response.output == {"allegations": []}
    assert script.calls == 6
    assert time.monotonic() - started >= 0.01  # waited at least one cooldown


async def test_rate_limits_until_the_deadline_raise_rate_limited_with_a_hint():
    settings = GatewaySettings(**{**FAST.__dict__, "call_deadline_sec": 0.2})
    script = Script(HTTPError(429, headers={"retry-after": "0.05"}))
    with pytest.raises(ModelRateLimitedError) as info:
        await _client(script, settings).complete(_req())
    assert info.value.retry_after_sec and info.value.retry_after_sec > 0


async def test_invalid_output_is_asked_again_then_fails_permanently():
    ok = Script("invalid", {"allegations": []})
    assert (await _client(ok).complete(_req())).output == {"allegations": []}

    bad = Script("invalid")
    with pytest.raises(ModelOutputInvalidError):
        await _client(bad).complete(_req())
    assert bad.calls == FAST.invalid_output_attempts


async def test_errors_and_logs_never_carry_the_exception_message(caplog):
    caplog.set_level(logging.DEBUG)
    for script in (Script(HTTPError(500)), Script(HTTPError(403)), Script("invalid")):
        with pytest.raises(Exception) as info:
            await _client(script).complete(_req())
        assert SECRET not in str(info.value)
    assert SECRET not in caplog.text


# --- shared gate ---------------------------------------------------------------------


async def test_concurrency_cap_holds_across_callers():
    script = Script(0.02)
    client = _client(script, GatewaySettings(**{**FAST.__dict__, "max_concurrency": 3}))
    await asyncio.gather(*(client.complete(_req()) for _ in range(12)))
    assert script.calls == 12
    assert script.peak == 3


async def test_one_429_pauses_every_caller():
    gate = RateGate(max_concurrency=8, cooldown_sec=0.05)
    gate.rate_limited()
    started = time.monotonic()
    async with gate.slot():
        pass
    assert time.monotonic() - started >= 0.045


def test_429s_in_one_episode_do_not_escalate_and_retry_after_wins():
    now = [0.0]
    gate = RateGate(max_concurrency=1, cooldown_sec=1.0, cooldown_max_sec=8.0, clock=lambda: now[0])
    assert gate.rate_limited() == 1.0
    assert gate.rate_limited() == 1.0  # in-flight call hitting the same episode
    assert gate.rate_limited(retry_after_sec=3.0) == 3.0
    now[0] = 3.5
    assert gate.rate_limited() == 2.0  # next episode escalates
    now[0] = 10.0
    gate.succeeded()
    assert gate.rate_limited() == 1.0


async def test_pacing_spaces_out_starts():
    gate = RateGate(max_concurrency=10, requests_per_minute=1200)  # one every 0.05s
    started = time.monotonic()
    for _ in range(4):
        async with gate.slot():
            pass
    assert time.monotonic() - started >= 0.14


def test_settings_from_env():
    s = GatewaySettings.from_env(
        {"CFR_LLM_MODEL_KEY": "2", "CFR_LLM_MAX_CONCURRENCY": "8", "CFR_LLM_REQUESTS_PER_MINUTE": "300"}
    )
    assert (s.model_key, s.max_concurrency, s.requests_per_minute) == ("2", 8, 300.0)
    assert GatewaySettings.from_env({}).requests_per_minute is None


# --- loop bridge ---------------------------------------------------------------------


def test_model_loop_serves_many_threads_through_one_client():
    loop = ModelLoop()
    try:
        script = Script(0.01)
        client = _client(script, GatewaySettings(**{**FAST.__dict__, "max_concurrency": 2}))
        results: list = []
        threads = [threading.Thread(target=lambda: results.append(loop.run(client.complete(_req())))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 6 and script.peak == 2

        with pytest.raises(ModelBlockedError):
            loop.run(_client(Script(HTTPError(403))).complete(_req()))
    finally:
        loop.close()
