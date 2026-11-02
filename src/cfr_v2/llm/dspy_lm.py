"""DSPy programs over the P5 model client (LDR-0015).

DSPy formats the prompt from a signature and parses the reply; everything else stays
with ``ModelClient`` (``GatewayModelClient`` in the worker): safechain, the shared rate
gate, retries, timeouts and the error taxonomy of LDR-0013. DSPy's own retries, cache
and call history are off, and litellm is never used: the LM runs on ``GatewayEngine``,
a dspy 3.4 custom engine (``complete(Request) -> Response``).

A DSPy call carries no case identity, so programs run through ``run_program``, which
sets the program and case for the engine's ``ModelRequest`` and maps DSPy's errors
back to ``ModelError``. A call made outside ``run_program`` is refused.

Only the async path is supported: stage handlers submit ``run_program`` to the
process's ``ModelLoop`` (``bridge.py``).
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import dspy
from dspy.lm15 import Message, Request, Response, Usage
from dspy.utils.exceptions import AdapterParseError

from .client import (
    ChatTurn,
    ModelClient,
    ModelError,
    ModelOutputInvalidError,
    ModelRequest,
    PermanentModelError,
    UnredactedPayloadError,
)


@dataclass(frozen=True)
class ProgramCall:
    """Who is calling: becomes the ``ModelRequest`` identity of every LM call in the program."""

    program_id: str
    program_version: str
    case_id: str
    task_id: str
    redacted: bool  # every input comes from redacted artifacts; the engine refuses False
    trace_id: str | None = None


_current_call: ContextVar[ProgramCall | None] = ContextVar("cfr_program_call", default=None)

# Structured output (json_schema) from the signature; no chat-format fallback call.
_ADAPTER = dspy.JSONAdapter()


class _Capabilities:
    # Read by DSPy from the LM's (sync) engine: the gateway accepts json_schema response formats.
    supports_response_schema = True
    supported_params = frozenset({"response_format"})


class GatewayEngine(_Capabilities):
    """Async DSPy engine: one ``ModelClient.complete`` per DSPy LM call."""

    def __init__(self, client: ModelClient) -> None:
        self.client = client

    async def complete(self, request: Request) -> Response:
        call = _current_call.get()
        if call is None:
            raise PermanentModelError("DSPy LM called outside run_program")
        if not call.redacted:
            raise UnredactedPayloadError(f"{call.program_id}: payload not marked redacted")
        schema = _output_schema(request.config.response_format)
        response = await self.client.complete(
            ModelRequest(
                program_id=call.program_id,
                program_version=call.program_version,
                case_id=call.case_id,
                task_id=call.task_id,
                instructions=_system_text(request.system),
                inputs={},
                messages=tuple(_turn(m) for m in request.messages),
                redacted=call.redacted,
                trace_id=call.trace_id,
                output_schema=schema,
            )
        )
        text = json.dumps(response.output, ensure_ascii=False) if schema is not None else str(response.output["text"])
        return Response(
            id=None,
            model=response.model_id,
            message=Message.assistant(text),
            finish_reason="stop",
            usage=Usage(input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens),
        )

    def stream(self, request: Request):
        raise NotImplementedError("GatewayEngine does not stream")

    async def aclose(self) -> None:
        return None


class _NoSyncEngine(_Capabilities):
    """DSPy needs a sync engine object; calls must go through ``run_program`` (async)."""

    def complete(self, request: Request) -> Response:
        raise PermanentModelError("synchronous DSPy calls are not supported; use run_program")

    def stream(self, request: Request):
        raise NotImplementedError("GatewayEngine does not stream")

    def close(self) -> None:
        return None


def gateway_lm(client: ModelClient, model_id: str) -> dspy.LM:
    """A DSPy LM whose every call is one ``client.complete``. Build one per process."""
    return dspy.LM(
        f"cfr-gateway/{model_id}",
        engine=_NoSyncEngine(),
        async_engine=GatewayEngine(client),
        cache=False,
        num_retries=0,  # the client retries
    )


async def run_program(program: dspy.Module, *, lm: dspy.LM, call: ProgramCall, **inputs: Any) -> dspy.Prediction:
    """Run a DSPy program for one case; failures surface as ``ModelError``.

    DSPy's parse errors quote the model's reply, so they are replaced, not chained.
    """
    token = _current_call.set(call)
    try:
        with dspy.context(lm=lm, adapter=_ADAPTER, disable_history=True):
            return await program.acall(**inputs)
    except Exception as exc:
        found = _model_error(exc)
        if found is not None:
            raise found from None
        if isinstance(exc, AdapterParseError):
            raise ModelOutputInvalidError(f"{call.program_id}: output did not match the signature") from None
        if type(exc).__module__.startswith("dspy"):
            raise PermanentModelError(f"{call.program_id}: {type(exc).__name__}") from None
        raise
    finally:
        _current_call.reset(token)


def _model_error(exc: BaseException) -> ModelError | None:
    """Our error under DSPy's wrapper (LMUnexpectedError and friends), if any."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ModelError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _output_schema(response_format: dict | None) -> dict | None:
    if response_format and response_format.get("type") == "json_schema":
        return response_format["schema"]
    return None  # plain text, or json_object mode: DSPy parses the text itself


def _text(parts: Any) -> str:
    if parts is None:
        return ""
    if isinstance(parts, str):
        return parts
    texts = []
    for part in parts:
        if getattr(part, "type", None) != "text":
            raise PermanentModelError(f"unsupported prompt part {getattr(part, 'type', type(part).__name__)!r}")
        texts.append(part.text)
    return "".join(texts)


def _system_text(system: Any) -> str:
    text = _text(system)
    if not text:
        raise PermanentModelError("DSPy request has no instructions")
    return text


def _turn(message: Message) -> ChatTurn:
    if message.role not in ("user", "assistant"):
        raise PermanentModelError(f"unsupported message role {message.role!r}")
    return ChatTurn(role=message.role, content=_text(message.parts))
