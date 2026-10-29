"""Deterministic fake model client for tests and offline development."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import JsonValue

from .client import ModelError, ModelRequest, ModelResponse, ModelUsage

FAKE_MODEL_ID = "fake-model"

Responder = Callable[[ModelRequest], Mapping[str, JsonValue]]


class FakeModelClient:
    """Answers from fixtures keyed by ``(program_id, input_hash)``, else a responder.

    Records every request so tests can assert on what was sent. Fixture values
    that are ``ModelError`` subclasses or instances are raised instead of returned.
    """

    def __init__(
        self,
        fixtures: Mapping[tuple[str, str], Mapping[str, JsonValue] | ModelError | type[ModelError]] | None = None,
        *,
        responder: Responder | None = None,
        tokens_per_char: float = 0.25,
    ) -> None:
        self.fixtures = dict(fixtures or {})
        self.responder = responder
        self.tokens_per_char = tokens_per_char
        self.requests: list[ModelRequest] = []

    @classmethod
    def from_file(cls, path: Path, **kwargs) -> "FakeModelClient":
        """Load ``[{"program_id": ..., "input_hash": ..., "output": {...}}, ...]``."""
        entries = json.loads(path.read_text())
        return cls({(e["program_id"], e["input_hash"]): e["output"] for e in entries}, **kwargs)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        key = (request.program_id, request.input_hash)
        if key in self.fixtures:
            answer = self.fixtures[key]
        elif self.responder is not None:
            answer = self.responder(request)
        else:
            raise LookupError(f"no fake response for {request.program_id} input {request.input_hash[:12]}")
        if isinstance(answer, type) and issubclass(answer, ModelError):
            raise answer(f"fake {answer.__name__}")
        if isinstance(answer, ModelError):
            raise answer
        output = dict(answer)
        prompt_chars = len(request.instructions) + len(json.dumps(request.inputs, sort_keys=True))
        usage = ModelUsage(
            input_tokens=int(prompt_chars * self.tokens_per_char),
            output_tokens=int(len(json.dumps(output, sort_keys=True)) * self.tokens_per_char),
        )
        return ModelResponse(output=output, usage=usage, model_id=FAKE_MODEL_ID)
