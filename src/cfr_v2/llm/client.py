"""P5 model-client interface, error taxonomy and budget accounting.

Components never talk to the gateway directly; they call a ``ModelClient``.
The gateway-backed client is in ``gateway.py``; tests use the fake.
See lld/modules/p5-llm-gateway/README.md and LDR-0013.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import Field, JsonValue, model_validator

from cfr_v2.contracts import Contract, ExecutionBudget, TaskStatus, canonical_hash


class ChatTurn(Contract):
    role: Literal["user", "assistant"]
    content: str


class ModelRequest(Contract):
    program_id: str = Field(min_length=1)
    program_version: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    # Transcript content goes in delimited data fields, never into instructions (HD-08).
    instructions: str = Field(min_length=1)
    inputs: dict[str, JsonValue]
    redacted: bool
    trace_id: str | None = None
    # JSON schema for structured output; None asks for plain text, returned as {"text": ...}.
    output_schema: dict[str, JsonValue] | None = None
    # A conversation already formatted by the caller (DSPy's adapter, LDR-0015), sent after the
    # instructions in place of the single user message built from ``inputs``.
    messages: tuple[ChatTurn, ...] = ()

    @model_validator(mode="after")
    def _messages_or_inputs(self) -> ModelRequest:
        if self.messages and self.inputs:
            raise ValueError("give either inputs or messages, not both")
        if self.messages and self.messages[-1].role != "user":
            raise ValueError("the conversation must end with a user turn")
        return self

    @property
    def input_hash(self) -> str:
        hashed = {
            "program": self.program_id,
            "version": self.program_version,
            "instructions": self.instructions,
            "inputs": self.inputs,
        }
        if self.output_schema is not None:  # absent from the hash when unset, so existing fixtures keep their keys
            hashed["output_schema"] = self.output_schema
        if self.messages:
            hashed["messages"] = [t.model_dump(mode="json") for t in self.messages]
        return canonical_hash(hashed)


class ModelUsage(Contract):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ModelResponse(Contract):
    output: dict[str, JsonValue]
    usage: ModelUsage
    model_id: str


class ModelError(RuntimeError):
    task_status: TaskStatus = TaskStatus.FAILED_PERMANENT
    error_code: str = "MODEL_ERROR"


class RetryableModelError(ModelError):
    """429, timeout or 5xx after the client's own retries; the runtime retries the run later."""

    task_status = TaskStatus.FAILED_RETRYABLE
    error_code = "MODEL_RETRYABLE"

    def __init__(self, message: str = "", *, retry_after_sec: float | None = None) -> None:
        super().__init__(message)
        # Earliest sensible retry, e.g. from a 429's Retry-After; the runtime waits at least this long.
        self.retry_after_sec = retry_after_sec


class ModelRateLimitedError(RetryableModelError):
    error_code = "MODEL_RATE_LIMITED"


class ModelTimeoutError(RetryableModelError):
    error_code = "MODEL_TIMEOUT"


class PermanentModelError(ModelError):
    """Retrying cannot help: rejected request, blocked content, invalid output, client bug."""

    error_code = "MODEL_PERMANENT"


class ModelBlockedError(PermanentModelError):
    """Firewall 403 or content filter (V1 recorded these and moved on)."""

    error_code = "MODEL_BLOCKED"


class ModelConfigError(PermanentModelError):
    """The model can't be used as configured (e.g. safechain redaction left on)."""

    error_code = "MODEL_CONFIG"


class ModelOutputInvalidError(PermanentModelError):
    """The model kept returning output that does not match ``output_schema``."""

    error_code = "MODEL_OUTPUT_INVALID"


class BudgetExhaustedError(ModelError):
    error_code = "BUDGET_EXHAUSTED"


class UnredactedPayloadError(ModelError):
    error_code = "UNREDACTED_PAYLOAD"


class ModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass
class BudgetTracker:
    """Per-case-run usage against an ``ExecutionBudget`` (HD-09). Thread-safe."""

    budget: ExecutionBudget
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve_call(self) -> None:
        with self._lock:
            limit = self.budget.max_model_calls
            if limit is not None and self.model_calls >= limit:
                raise BudgetExhaustedError(f"model call limit {limit} reached")
            self.model_calls += 1

    def record(self, usage: ModelUsage) -> None:
        with self._lock:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            over = []
            if self.budget.max_input_tokens is not None and self.input_tokens > self.budget.max_input_tokens:
                over.append("input")
            if self.budget.max_output_tokens is not None and self.output_tokens > self.budget.max_output_tokens:
                over.append("output")
        if over:
            raise BudgetExhaustedError(f"{'/'.join(over)} token budget exceeded")


class GuardedModelClient:
    """Wraps any client with redaction enforcement and budget accounting."""

    def __init__(self, inner: ModelClient, tracker: BudgetTracker) -> None:
        self.inner = inner
        self.tracker = tracker

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.redacted:
            raise UnredactedPayloadError(f"{request.program_id}: payload not marked redacted")
        self.tracker.reserve_call()
        response = await self.inner.complete(request)
        self.tracker.record(response.usage)
        return response
