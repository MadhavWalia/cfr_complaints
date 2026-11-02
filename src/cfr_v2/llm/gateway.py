"""Model client backed by the enterprise GenAI gateway through safechain (LDR-0013).

V1 (``cfr-v1/src/chc/query_decomposition.py``) builds a model per call with
``safechain.core.model.amodel("3", params, elf_headers())``. That builder is
synchronous despite its name (it reads ee-config and fetches an IDaaS token),
and the model it returns refreshes its own token when it expires, so here one
model per process serves every call: built off the event loop, rebuilt only
after a 401. Trace headers go on each request as ``extra_headers``; safechain
merges build-time headers into the shared config, so they would stick. The
openai SDK's own retries are turned off (``max_retries=0``) so they don't stack
on these:

- every failure is classified once (``classify``) from the HTTP status, falling
  back to V1's message checks;
- 429s pause the whole process through the shared ``RateGate`` and don't use up
  attempts; the call deadline bounds them;
- timeouts, 5xx, connection errors and 401s (the model is rebuilt for a fresh
  token) retry with jittered exponential backoff;
- 403 firewall / content-filter blocks (on the prompt or the output), refusals,
  other 4xx and client or config errors fail at once;
- exhausting retries raises ``RetryableModelError`` subclasses, which the runtime
  turns into a delayed retry of the whole run rather than a failure.

Redaction happens at intake (C01), so safechain's own redaction must be
``disabled`` in the model config: masking again would shift the text the model
quotes away from the stored redacted transcript. The client refuses a model
with redaction on.

Logs and spans carry the program, status and exception type only, never the
exception message (it can quote the prompt) or any request content.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from cfr_v2.telemetry import elf_headers, record_llm_call, set_attributes, span

from .client import (
    ModelBlockedError,
    ModelConfigError,
    ModelError,
    ModelOutputInvalidError,
    ModelRateLimitedError,
    ModelRequest,
    ModelResponse,
    ModelTimeoutError,
    ModelUsage,
    PermanentModelError,
    RetryableModelError,
)
from .limiter import RateGate
from .settings import GatewaySettings

logger = logging.getLogger(__name__)

# (model_key, params) -> LangChain chat model (safechain's SafeAzureChatOpenAI) with ``ainvoke``.
ModelFactory = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def safechain_model(model_key: str, params: dict[str, Any]) -> Any:
    """safechain's model for ``model_key``, built on a worker thread (the builder blocks).

    IDaaS auth, token refresh, the amexcerts CA bundle and gateway routing stay inside safechain.
    """
    from safechain.core.model import model

    from cfr_v2.telemetry import safechain_httpx_kwargs

    return await asyncio.to_thread(model, model_key, params | safechain_httpx_kwargs())


class Failure(enum.Enum):
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSIENT = "transient"  # 5xx, 408/409, connection errors, unknown runtime errors
    AUTH = "auth"  # 401: gateway token blips; the model is rebuilt with a fresh token
    BLOCKED = "blocked"  # 403 firewall, content filter
    REJECTED = "rejected"  # other 4xx: the request itself is wrong
    BUG = "bug"  # programming or configuration error on our side


RETRIED = frozenset({Failure.RATE_LIMITED, Failure.TIMEOUT, Failure.TRANSIENT, Failure.AUTH})


@dataclass(frozen=True)
class Classified:
    failure: Failure
    status: int | None = None
    retry_after_sec: float | None = None


_TRANSIENT_STATUSES = frozenset({408, 409, 500, 502, 503, 504})
_TRANSIENT_TYPES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
    }
)
_BUG_TYPES = (TypeError, AttributeError, KeyError, NameError, NotImplementedError, ImportError, FileNotFoundError)
# Raised while building the model: unknown model key, missing IDaaS section, ...
_BUG_TYPE_NAMES = frozenset({"ConfigValidationError"})
# V1's message checks, kept as a fallback for wrappers that drop the status code.
_MSG_RATE_LIMITED = re.compile(r"\b429\b|Too Many Requests", re.IGNORECASE)
_MSG_BLOCKED = re.compile(r"\b403\b|Forbidden|Firewall", re.IGNORECASE)
_MSG_AUTH = re.compile(r"\b401\b|Unauthorized", re.IGNORECASE)
_MSG_TRANSIENT = re.compile(
    r"Could not contact endpoint|timed out|timeout|connection (reset|refused|aborted)", re.IGNORECASE
)


def _status(exc: BaseException) -> int | None:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(exc, "http_status", None),
        getattr(exc, "status", None),
    ):
        if isinstance(candidate, int):
            return candidate
    return None


def _retry_after(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        if (ms := headers.get("retry-after-ms")) is not None:
            return max(0.0, float(ms) / 1000)
        if (sec := headers.get("retry-after")) is not None:
            return max(0.0, float(sec))
    except (TypeError, ValueError):  # an HTTP-date; the cooldown default applies
        return None
    return None


def _content_filtered(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    return code == "content_filter" or "content_filter" in str(exc) or "ResponsibleAIPolicyViolation" in str(exc)


def classify(exc: BaseException) -> Classified:
    """Map a gateway/safechain/openai exception to a failure kind. Inspects, never logs, the message."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or type(exc).__name__ in {
        "APITimeoutError",
        "ReadTimeout",
    }:
        return Classified(Failure.TIMEOUT, _status(exc))
    status = _status(exc)
    if status == 429:
        return Classified(Failure.RATE_LIMITED, status, _retry_after(exc))
    if status == 401:
        return Classified(Failure.AUTH, status)
    if status == 403:
        return Classified(Failure.BLOCKED, status)
    if status is not None and (status in _TRANSIENT_STATUSES or status >= 500):
        return Classified(Failure.TRANSIENT, status)
    if status == 400 and _content_filtered(exc):
        return Classified(Failure.BLOCKED, status)
    if status is not None and 400 <= status < 500:
        return Classified(Failure.REJECTED, status)
    if type(exc).__name__ in _TRANSIENT_TYPES or isinstance(exc, ConnectionError):
        return Classified(Failure.TRANSIENT)
    if isinstance(exc, _BUG_TYPES) or type(exc).__name__ in _BUG_TYPE_NAMES:
        return Classified(Failure.BUG)
    message = str(exc)
    if _MSG_RATE_LIMITED.search(message):
        return Classified(Failure.RATE_LIMITED, None, _retry_after(exc))
    if _MSG_BLOCKED.search(message):
        return Classified(Failure.BLOCKED)
    if _MSG_AUTH.search(message):
        return Classified(Failure.AUTH)
    if _MSG_TRANSIENT.search(message):
        return Classified(Failure.TRANSIENT)
    return Classified(Failure.TRANSIENT)  # unknown runtime error: bounded retries, then the run is retried


class _InvalidOutput(Exception):
    """The response did not parse against ``output_schema``."""


_EXHAUSTED: dict[Failure, type[RetryableModelError]] = {
    Failure.RATE_LIMITED: ModelRateLimitedError,
    Failure.TIMEOUT: ModelTimeoutError,
}
_FATAL: dict[Failure, type[PermanentModelError]] = {Failure.BLOCKED: ModelBlockedError}


class GatewayModelClient:
    """``ModelClient`` over the gateway. Use one instance per process, from one event loop (``ModelLoop``)."""

    def __init__(
        self,
        settings: GatewaySettings | None = None,
        *,
        model_factory: ModelFactory = safechain_model,
        gate: RateGate | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings or GatewaySettings.from_env()
        s = self.settings
        self.gate = gate or RateGate(
            max_concurrency=s.max_concurrency,
            requests_per_minute=s.requests_per_minute,
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

    async def complete(self, request: ModelRequest) -> ModelResponse:
        s = self.settings
        deadline = self._clock() + s.call_deadline_sec
        tries = failures = invalid = 0
        while True:
            tries += 1
            try:
                response = await self._attempt(request, deadline, tries)
            except _InvalidOutput as exc:
                invalid += 1
                if invalid >= s.invalid_output_attempts:
                    raise ModelOutputInvalidError(
                        f"{request.program_id}: output did not match its schema in {invalid} responses"
                    ) from exc
                logger.warning(
                    "llm: %s case %s returned schema-invalid output; asking again", request.program_id, request.case_id
                )
                continue
            except ModelError:  # already decided: blocked output, refusal, misconfigured model
                raise
            except Exception as exc:
                found = classify(exc)
                detail = _describe(found, exc)
                if found.failure is Failure.AUTH:
                    self._model = None  # next attempt builds a model with a fresh token
                if found.failure not in RETRIED:
                    raise _FATAL.get(found.failure, PermanentModelError)(
                        f"{request.program_id}: {found.failure.value} ({detail})"
                    ) from exc
                if found.failure is Failure.RATE_LIMITED:
                    wait = self.gate.rate_limited(found.retry_after_sec)  # every caller waits it out in slot()
                else:
                    failures += 1
                    cap = min(s.backoff_max_sec, s.backoff_base_sec * 2 ** (failures - 1))
                    wait = random.uniform(cap / 2, cap)
                exhausted = failures >= s.max_attempts or self._clock() + wait >= deadline
                if exhausted:
                    raise _EXHAUSTED.get(found.failure, RetryableModelError)(
                        f"{request.program_id}: {found.failure.value} after {tries} tries ({detail})",
                        retry_after_sec=wait,
                    ) from exc
                logger.warning(
                    "llm: %s case %s try %d %s (%s); retrying in %.1fs",
                    request.program_id,
                    request.case_id,
                    tries,
                    found.failure.value,
                    detail,
                    wait,
                )
                if found.failure is not Failure.RATE_LIMITED:
                    await self._sleep(wait)
                continue
            self.gate.succeeded()
            return response

    async def _attempt(self, request: ModelRequest, deadline: float, tries: int) -> ModelResponse:
        s = self.settings
        async with self.gate.slot():
            timeout = min(s.attempt_timeout_sec, deadline - self._clock())
            if timeout <= 0:
                raise TimeoutError("call deadline reached while waiting for a slot")
            with (
                span(
                    "llm.call",
                    program=request.program_id,
                    program_version=request.program_version,
                    model=s.model_id,
                    attempt=tries,
                ) as call_span,
                record_llm_call(s.model_id, operation=request.program_id),
            ):
                try:
                    response = await asyncio.wait_for(self._invoke(request), timeout)
                except _InvalidOutput:
                    set_attributes(call_span, outcome="invalid_output")
                    raise
                except ModelError as exc:
                    set_attributes(call_span, outcome=exc.error_code.lower())
                    raise
                except Exception as exc:
                    found = classify(exc)
                    set_attributes(call_span, outcome=found.failure.value, http_status=found.status)
                    raise
                set_attributes(
                    call_span,
                    outcome="ok",
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
                return response

    async def _get_model(self) -> Any:
        if self._model is None:
            async with self._model_lock:
                if self._model is None:
                    llm = await self._model_factory(self.settings.model_key, self.settings.model_params())
                    mode = getattr(getattr(llm, "redaction", None), "mode", None)
                    if mode not in (None, "disabled"):
                        raise ModelConfigError(
                            f"model {self.settings.model_key!r} has safechain redaction {mode!r}; set "
                            "`redaction: mode: disabled` for it in CONFIG_PATH (text is redacted at intake)"
                        )
                    self._model = llm
        return self._model

    async def _invoke(self, request: ModelRequest) -> ModelResponse:
        s = self.settings
        llm = await self._get_model()
        # Transcript content stays in the data message, never in the instructions (HD-08).
        messages = [{"role": "system", "content": request.instructions}]
        if request.messages:
            messages += [t.model_dump() for t in request.messages]
        else:
            messages.append({"role": "user", "content": json.dumps(request.inputs, ensure_ascii=False, sort_keys=True)})
        kwargs: dict[str, Any] = {}
        if headers := elf_headers():  # this call's trace context, for gateway-side correlation
            kwargs["extra_headers"] = headers
        if request.output_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _schema_name(request),
                    "schema": request.output_schema,
                    "strict": s.strict_schema,
                },
            }
        message = await llm.ainvoke(messages, **kwargs)

        metadata = getattr(message, "response_metadata", None) or {}
        if metadata.get("finish_reason") == "content_filter":
            raise ModelBlockedError(f"{request.program_id}: output blocked by the content filter")
        if (getattr(message, "additional_kwargs", None) or {}).get("refusal"):
            raise ModelBlockedError(f"{request.program_id}: the model refused")
        text = _text(message)
        if request.output_schema is None:
            return ModelResponse(output={"text": text}, usage=_usage(message), model_id=s.model_id)
        try:
            parsed = json.loads(text)
        except ValueError:
            raise _InvalidOutput() from None  # truncated (finish_reason "length") or not JSON
        if not isinstance(parsed, dict):
            raise _InvalidOutput()
        return ModelResponse(output=parsed, usage=_usage(message), model_id=s.model_id)


def _schema_name(request: ModelRequest) -> str:
    title = (request.output_schema or {}).get("title") or request.program_id
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(title))[:64]


def _text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):  # content blocks
        return "".join(b if isinstance(b, str) else str(b.get("text", "")) for b in content)
    return str(content)


def _usage(message: Any) -> ModelUsage:
    meta = getattr(message, "usage_metadata", None) or {}
    if meta:
        return ModelUsage(input_tokens=meta.get("input_tokens", 0), output_tokens=meta.get("output_tokens", 0))
    token_usage = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
    return ModelUsage(
        input_tokens=token_usage.get("prompt_tokens", 0), output_tokens=token_usage.get("completion_tokens", 0)
    )


def _describe(found: Classified, exc: BaseException) -> str:
    status = f"HTTP {found.status}, " if found.status is not None else ""
    return f"{status}{type(exc).__name__}"
