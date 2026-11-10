"""Embeddings through the gateway (LDR-0019), with the same failure handling as chat calls (LDR-0013).

safechain's embedding model (key ``"4"`` in V1, text-embedding-3-large) has a synchronous
``embed_documents``, as V1 uses it, so each call runs on a worker thread. Failures are classified
by ``classify``: 429s pause every call through the rate gate, transient errors back off and retry,
401s rebuild the model for a fresh token, and firewall blocks fail at once.

Only redacted text may be sent. Logs and spans carry the purpose and status only, never text.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from cfr_v2.telemetry import get_logger, record_llm_call, record_retry, set_attributes, span

from .client import (
    ModelBlockedError,
    ModelConfigError,
    ModelError,
    ModelOutputInvalidError,
    PermanentModelError,
    RetryableModelError,
    UnredactedPayloadError,
)
from .gateway import _EXHAUSTED, _FATAL, RETRIED, Failure, _describe, classify
from .limiter import RateGate

logger = get_logger(__name__)

PROGRAM_VERSION = "embed-1"

EmbeddingFactory = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def safechain_embeddings(model_key: str, params: dict[str, Any]) -> Any:
    from safechain.core.model import model

    return await asyncio.to_thread(model, model_key, params)


@dataclass(frozen=True)
class EmbeddingSettings:
    model_key: str = "4"  # under `models:` in config_gpt.yml
    model_id: str = "text-embedding-3-large"
    dimensions: int = 3072
    batch_size: int = 64  # as V1
    max_concurrency: int = 5  # as V1
    max_attempts: int = 5
    backoff_base_sec: float = 2.0
    backoff_max_sec: float = 30.0
    attempt_timeout_sec: float = 120.0
    call_deadline_sec: float = 600.0
    rate_limit_cooldown_sec: float = 5.0
    rate_limit_cooldown_max_sec: float = 60.0

    def model_params(self) -> dict[str, Any]:
        """As for chat, retries and timeouts are this client's job. It matters more here: a call
        runs on a thread that outlives its timeout, so the SDK would go on retrying a call we had
        given up on, past the concurrency limit."""
        return {"max_retries": 0, "request_timeout": self.attempt_timeout_sec}

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> EmbeddingSettings:
        d = cls()
        return cls(
            model_key=env.get("CFR_EMBEDDING_MODEL_KEY", d.model_key),
            model_id=env.get("CFR_EMBEDDING_MODEL_ID", d.model_id),
            dimensions=int(env.get("CFR_EMBEDDING_DIMENSIONS", d.dimensions)),
            batch_size=int(env.get("CFR_EMBEDDING_BATCH_SIZE", d.batch_size)),
            max_concurrency=int(env.get("CFR_EMBEDDING_MAX_CONCURRENCY", d.max_concurrency)),
            attempt_timeout_sec=float(env.get("CFR_EMBEDDING_ATTEMPT_TIMEOUT_SEC", d.attempt_timeout_sec)),
            call_deadline_sec=float(env.get("CFR_EMBEDDING_CALL_DEADLINE_SEC", d.call_deadline_sec)),
        )


class EmbeddingClient(Protocol):
    model_id: str
    dimensions: int

    async def embed(self, texts: Sequence[str], *, purpose: str, redacted: bool) -> list[list[float]]: ...


async def embed_allowing_blocks(
    client: EmbeddingClient, texts: Sequence[str], *, purpose: str, redacted: bool
) -> list[list[float] | None]:
    """Like ``embed``, but a blocked text gives ``None`` instead of failing the rest.

    A block names no text, so the batch is halved until the blocked ones are found.
    """
    if not texts:
        return []
    try:
        return list(await client.embed(texts, purpose=purpose, redacted=redacted))
    except ModelBlockedError:
        if len(texts) == 1:
            return [None]
    middle = len(texts) // 2
    first = await embed_allowing_blocks(client, texts[:middle], purpose=purpose, redacted=redacted)
    return first + await embed_allowing_blocks(client, texts[middle:], purpose=purpose, redacted=redacted)


class GatewayEmbeddingClient:
    """Use one instance per process, from one event loop (``ModelLoop``)."""

    def __init__(
        self,
        settings: EmbeddingSettings | None = None,
        *,
        model_factory: EmbeddingFactory = safechain_embeddings,
        gate: RateGate | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = s = settings or EmbeddingSettings.from_env()
        self.model_id = s.model_id
        self.dimensions = s.dimensions
        self.gate = gate or RateGate(
            max_concurrency=s.max_concurrency,
            requests_per_minute=None,
            cooldown_sec=s.rate_limit_cooldown_sec,
            cooldown_max_sec=s.rate_limit_cooldown_max_sec,
            clock=clock,
            sleep=sleep,
        )
        self._model_factory = model_factory
        self._model: Any = None
        self._model_lock = asyncio.Lock()
        self._clock = clock
        self._sleep = sleep

    async def embed(self, texts: Sequence[str], *, purpose: str, redacted: bool) -> list[list[float]]:
        if not redacted:
            raise UnredactedPayloadError(f"{purpose}: payload not marked redacted")
        size = self.settings.batch_size
        vectors: list[list[float]] = []
        for start in range(0, len(texts), size):
            vectors += await self._call(list(texts[start : start + size]), purpose)
        return vectors

    async def _call(self, batch: list[str], purpose: str) -> list[list[float]]:
        s = self.settings
        deadline = self._clock() + s.call_deadline_sec
        tries = failures = 0
        while True:
            tries += 1
            try:
                vectors = await self._attempt(batch, purpose, deadline, tries)
            except ModelError:
                raise
            except Exception as exc:
                found = classify(exc)
                detail = _describe(found, exc)
                if found.failure is Failure.AUTH:
                    self._model = None
                if found.failure not in RETRIED:
                    raise _FATAL.get(found.failure, PermanentModelError)(
                        f"{purpose}: {found.failure.value} ({detail})"
                    ) from exc
                if found.failure is Failure.RATE_LIMITED:
                    wait = self.gate.rate_limited(found.retry_after_sec)
                else:
                    failures += 1
                    cap = min(s.backoff_max_sec, s.backoff_base_sec * 2 ** (failures - 1))
                    wait = random.uniform(cap / 2, cap)
                if failures >= s.max_attempts or self._clock() + wait >= deadline:
                    raise _EXHAUSTED.get(found.failure, RetryableModelError)(
                        f"{purpose}: {found.failure.value} after {tries} tries ({detail})", retry_after_sec=wait
                    ) from exc
                record_retry("embedding", found.failure.value, found.status)
                logger.warning("embed: %s try %d %s (%s); retrying", purpose, tries, found.failure.value, detail)
                if found.failure is not Failure.RATE_LIMITED:
                    await self._sleep(wait)
                continue
            self.gate.succeeded()
            return vectors

    async def _attempt(self, batch: list[str], purpose: str, deadline: float, tries: int) -> list[list[float]]:
        s = self.settings
        queued = self._clock()
        async with self.gate.slot():
            queue_wait = self._clock() - queued
            timeout = min(s.attempt_timeout_sec, deadline - self._clock())
            if timeout <= 0:
                raise TimeoutError("call deadline reached while waiting for a slot")
            with (
                span(
                    "llm.call",
                    program=purpose,
                    program_version=PROGRAM_VERSION,
                    model=s.model_id,
                    attempt=tries,
                    queue_wait_sec=round(queue_wait, 3),
                ) as call_span,
                record_llm_call(s.model_id, operation=purpose),
            ):
                try:
                    model = await self._get_model()
                    vectors = await asyncio.wait_for(asyncio.to_thread(model.embed_documents, batch), timeout)
                except ModelError as exc:
                    set_attributes(call_span, outcome=exc.error_code.lower())
                    raise
                except Exception as exc:
                    found = classify(exc)
                    set_attributes(call_span, outcome=found.failure.value, http_status=found.status)
                    raise
                if len(vectors) != len(batch) or any(len(v) != s.dimensions for v in vectors):
                    set_attributes(call_span, outcome="invalid_output")
                    raise ModelOutputInvalidError(f"{purpose}: expected {len(batch)} vectors of {s.dimensions}")
                set_attributes(call_span, outcome="ok")
                return vectors

    async def _get_model(self) -> Any:
        if self._model is None:
            async with self._model_lock:
                if self._model is None:
                    model = await self._model_factory(self.settings.model_key, self.settings.model_params())
                    mode = getattr(getattr(model, "redaction", None), "mode", None)
                    if mode not in (None, "disabled"):
                        raise ModelConfigError(
                            f"model {self.settings.model_key!r} has safechain redaction {mode!r}; set "
                            "`redaction: mode: disabled` for it in CONFIG_PATH (text is already redacted)"
                        )
                    self._model = model
        return self._model
