"""Embeddings through the gateway (LDR-0019): retries, blocks, redaction, and what reaches logs."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from cfr_v2.llm import (
    EmbeddingSettings,
    FakeEmbeddingClient,
    GatewayEmbeddingClient,
    ModelBlockedError,
    ModelRateLimitedError,
    RetryableModelError,
    UnredactedPayloadError,
    embed_allowing_blocks,
)
from cfr_v2.llm.client import ModelConfigError, ModelOutputInvalidError

SECRET = "SECRET-TEXT"
SETTINGS = EmbeddingSettings(dimensions=3, batch_size=2, max_attempts=2, backoff_base_sec=0.0, backoff_max_sec=0.0)


class HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"{status} about {SECRET}")
        self.status_code = status


class FakeEmbeddings:
    def __init__(self, *failures, mode="disabled", size=3) -> None:
        self.failures = list(failures)
        self.batches: list[list[str]] = []
        self.size = size
        self.redaction = SimpleNamespace(mode=mode)

    def embed_documents(self, batch):
        self.batches.append(batch)
        if self.failures:
            raise self.failures.pop(0)
        return [[1.0] * self.size for _ in batch]


def _client(model, **kwargs):
    built = []

    async def factory(key, params):
        built.append(key)
        return model

    async def no_sleep(_):
        return None

    client = GatewayEmbeddingClient(kwargs.pop("settings", SETTINGS), model_factory=factory, sleep=no_sleep, **kwargs)
    return client, built


def _embed(client, texts=("a", "b", "c")):
    return asyncio.run(client.embed(list(texts), purpose="test.embed", redacted=True))


def test_texts_go_in_batches_and_keep_their_order():
    model = FakeEmbeddings()
    client, built = _client(model)
    assert len(_embed(client)) == 3
    assert model.batches == [["a", "b"], ["c"]]
    assert built == ["4"]


def test_unredacted_text_is_refused_before_any_call():
    model = FakeEmbeddings()
    client, _ = _client(model)
    with pytest.raises(UnredactedPayloadError):
        asyncio.run(client.embed(["a"], purpose="test.embed", redacted=False))
    assert model.batches == []


def _retries(points) -> int:
    want = {"stage": "embedding", "outcome": "transient", "status": "503"}
    return sum(v for a, v in points.get("cfr.llm.retries", []) if want.items() <= dict(a).items())


def test_a_transient_failure_is_retried_and_counted(metric_points):
    before = _retries(metric_points())
    model = FakeEmbeddings(HttpError(503))
    client, _ = _client(model)
    assert len(_embed(client, ["a"])) == 1
    assert len(model.batches) == 2
    assert _retries(metric_points()) == before + 1


def test_retries_run_out():
    client, _ = _client(FakeEmbeddings(HttpError(503), HttpError(503)))
    with pytest.raises(RetryableModelError):
        _embed(client, ["a"])


def test_a_401_rebuilds_the_model():
    client, built = _client(FakeEmbeddings(HttpError(401)))
    _embed(client, ["a"])
    assert len(built) == 2


def test_a_403_fails_at_once():
    model = FakeEmbeddings(HttpError(403))
    client, _ = _client(model)
    with pytest.raises(ModelBlockedError):
        _embed(client, ["a"])
    assert len(model.batches) == 1


def test_a_429_goes_through_the_rate_gate_until_the_deadline():
    settings = EmbeddingSettings(dimensions=3, call_deadline_sec=0.0, rate_limit_cooldown_sec=1.0)
    client, _ = _client(FakeEmbeddings(HttpError(429)), settings=settings)
    with pytest.raises((ModelRateLimitedError, RetryableModelError)):
        _embed(client, ["a"])


def test_safechain_redaction_left_on_is_refused():
    client, _ = _client(FakeEmbeddings(mode="masking"))
    with pytest.raises(ModelConfigError):
        _embed(client, ["a"])


def test_vectors_of_the_wrong_size_are_refused():
    client, _ = _client(FakeEmbeddings(size=2))
    with pytest.raises(ModelOutputInvalidError):
        _embed(client, ["a"])


def test_logs_never_carry_the_exception_message(caplog):
    client, _ = _client(FakeEmbeddings(HttpError(503)))
    with caplog.at_level(logging.DEBUG):
        _embed(client, ["a"])
    assert "HTTP 503" in caplog.text and SECRET not in caplog.text


def test_only_the_blocked_text_is_lost():
    client = FakeEmbeddingClient(dimensions=8, blocked={"bad"})
    texts = ["one", "two", "a bad text", "four", "five"]
    vectors = asyncio.run(embed_allowing_blocks(client, texts, purpose="test.embed", redacted=True))
    assert [v is None for v in vectors] == [False, False, True, False, False]


def test_nothing_to_embed_makes_no_call():
    client = FakeEmbeddingClient()
    assert asyncio.run(embed_allowing_blocks(client, [], purpose="test.embed", redacted=True)) == []
    assert client.calls == []


def test_settings_from_the_environment():
    settings = EmbeddingSettings.from_env({"CFR_EMBEDDING_MODEL_KEY": "7", "CFR_EMBEDDING_BATCH_SIZE": "16"})
    assert (settings.model_key, settings.batch_size, settings.dimensions) == ("7", 16, 3072)


def test_the_sdk_leaves_retries_and_timeouts_to_this_client():
    """A call outlives its timeout on its thread, so SDK retries would run unseen past the limit."""
    seen = {}

    async def factory(key, params):
        seen.update(params)
        return FakeEmbeddings()

    client = GatewayEmbeddingClient(SETTINGS, model_factory=factory)
    asyncio.run(client.embed(["a"], purpose="p", redacted=True))
    assert seen == {"max_retries": 0, "request_timeout": SETTINGS.attempt_timeout_sec}


def test_the_attempt_timeout_can_be_set():
    assert EmbeddingSettings.from_env({"CFR_EMBEDDING_ATTEMPT_TIMEOUT_SEC": "45"}).attempt_timeout_sec == 45


def test_the_call_deadline_can_be_set():
    assert EmbeddingSettings.from_env({"CFR_EMBEDDING_CALL_DEADLINE_SEC": "900"}).call_deadline_sec == 900


def test_a_call_span_says_how_long_it_queued(spans):
    client, _ = _client(FakeEmbeddings())
    asyncio.run(client.embed(["a"], purpose="p", redacted=True))
    (call,) = [s for s in spans.get_finished_spans() if s.name == "llm.call"]
    assert call.attributes["cfr.queue_wait_sec"] >= 0
