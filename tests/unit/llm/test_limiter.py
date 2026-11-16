"""The adaptive rate gate (LDR-0032): a slow start instead of a burst, halving on a 429, no herd."""

from __future__ import annotations

import asyncio

import pytest

from cfr_v2.llm import GatewaySettings
from cfr_v2.llm.embeddings import EmbeddingSettings
from cfr_v2.llm.limiter import RateGate


def _round(gate: RateGate) -> None:
    """One round of successes: as many as the calls allowed at once."""
    for _ in range(gate.limit):
        gate.succeeded()


def test_a_slow_start_doubles_each_round_up_to_the_cap():
    gate = RateGate(max_concurrency=20, start_concurrency=2)
    seen = [gate.limit]
    for _ in range(5):
        _round(gate)
        seen.append(gate.limit)
    assert seen == [2, 4, 8, 16, 20, 20]


def test_without_a_start_the_gate_allows_the_cap_at_once_as_before():
    assert RateGate(max_concurrency=8).limit == 8
    assert RateGate(max_concurrency=4, start_concurrency=10).limit == 4


def test_a_429_halves_the_allowance_once_per_episode_then_it_grows_by_one():
    now = [0.0]
    gate = RateGate(max_concurrency=32, start_concurrency=16, cooldown_sec=1.0, clock=lambda: now[0])
    gate.rate_limited()
    assert gate.limit == 8
    gate.rate_limited()  # another call in flight hitting the same episode
    assert gate.limit == 8
    now[0] = 5.0
    gate.rate_limited()  # a new episode
    assert gate.limit == 4
    now[0] = 10.0
    seen = []
    for _ in range(3):
        _round(gate)
        seen.append(gate.limit)
    assert seen == [5, 6, 7]  # past the point of the last 429: carefully, one more a round


def test_the_allowance_never_drops_below_one():
    now = [0.0]
    gate = RateGate(max_concurrency=4, start_concurrency=1, cooldown_sec=0.1, clock=lambda: now[0])
    for i in range(5):
        now[0] = i * 10.0
        gate.rate_limited()
    assert gate.limit == 1


async def test_no_more_calls_run_at_once_than_allowed_and_more_once_it_grows():
    gate = RateGate(max_concurrency=8, start_concurrency=2)
    release = asyncio.Event()
    peak = [0]

    async def call():
        async with gate.slot():
            peak[0] = max(peak[0], gate.in_flight)
            await release.wait()
        gate.succeeded()

    tasks = [asyncio.create_task(call()) for _ in range(10)]
    await asyncio.sleep(0.01)
    assert gate.in_flight == 2 and peak[0] == 2  # the start: two, not ten
    release.set()
    await asyncio.gather(*tasks)
    assert gate.in_flight == 0 and gate.limit > 2


async def test_a_waiting_call_that_is_cancelled_keeps_no_slot():
    gate = RateGate(max_concurrency=1)
    hold = asyncio.Event()

    async def holder():
        async with gate.slot():
            await hold.wait()

    first = asyncio.create_task(holder())
    await asyncio.sleep(0)
    waiting = asyncio.create_task(holder())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    hold.set()
    await first
    assert gate.in_flight == 0
    async with gate.slot():  # the slot is free again
        assert gate.in_flight == 1


async def test_calls_resume_spread_after_a_cooldown_never_for_longer_than_it():
    now = [0.0]
    slept: list[float] = []
    drawn: list[tuple[float, float]] = []

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    def jitter(low, high):
        drawn.append((low, high))
        return high

    gate = RateGate(
        max_concurrency=4, cooldown_sec=0.5, resume_jitter_sec=2.0, clock=lambda: now[0], sleep=sleep, jitter=jitter
    )
    gate.rate_limited()
    async with gate.slot():
        pass
    assert drawn == [(0.0, 0.5)]  # spread over at most the cooldown itself, not the 2 s cap
    assert slept == [0.5, 0.5]
    assert gate.resume_by(0.5) == 1.0 and gate.resume_by(10.0) == 12.0
    drawn.clear()
    async with gate.slot():  # no cooldown: no jitter either
        pass
    assert drawn == []


def test_the_clients_start_low_and_the_start_can_be_set():
    assert (GatewaySettings().start_concurrency, GatewaySettings().max_concurrency) == (4, 32)
    assert GatewaySettings.from_env({"CFR_LLM_START_CONCURRENCY": "8"}).start_concurrency == 8
    assert EmbeddingSettings().start_concurrency == 2
    assert EmbeddingSettings.from_env({"CFR_EMBEDDING_START_CONCURRENCY": "1"}).start_concurrency == 1
    with pytest.raises(ValueError):
        RateGate(max_concurrency=4, start_concurrency=0)


def test_the_allowance_is_a_metric_by_kind(metric_points):
    gate = RateGate(max_concurrency=6, start_concurrency=3, name="embedding")
    _round(gate)
    values = [v for a, v in metric_points()["cfr.llm.concurrency_limit"] if dict(a) == {"kind": "embedding"}]
    assert values and values[-1] == 6


def test_the_real_clients_build_their_gates_from_these_settings():
    from cfr_v2.llm import GatewayModelClient
    from cfr_v2.llm.embeddings import GatewayEmbeddingClient

    async def factory(key, params):  # never called: no model is built here
        raise AssertionError

    chat = GatewayModelClient(GatewaySettings(), model_factory=factory)
    assert (chat.gate.name, chat.gate.limit, chat.gate.max_concurrency) == ("chat", 4, 32)
    embed = GatewayEmbeddingClient(EmbeddingSettings(), model_factory=factory)
    assert (embed.gate.name, embed.gate.limit, embed.gate.max_concurrency) == ("embedding", 2, 5)
