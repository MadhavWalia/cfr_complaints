"""The gateway smoke test's checks, run against a fake safechain model (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from cfr_v2.llm import GatewaySettings, close_model_loop
from cfr_v2.llm.smoke import run_checks


class FakeChat:
    def __init__(self, strict_ok: bool) -> None:
        self.redaction = SimpleNamespace(mode="disabled")
        self.strict_ok = strict_ok

    async def ainvoke(self, messages, **kwargs):
        fmt = kwargs.get("response_format")
        if fmt and fmt["json_schema"]["strict"] and not self.strict_ok:
            raise type("BadRequestError", (Exception,), {"status_code": 400})("strict not supported")
        content = json.dumps({"colour": "blue", "legs": 4}) if fmt else "Blue; four."
        return SimpleNamespace(
            content=content,
            usage_metadata={"input_tokens": 40, "output_tokens": 8},
            response_metadata={"finish_reason": "stop"},
            additional_kwargs={},
        )


def _run(strict_ok: bool):
    async def factory(key, params):
        return FakeChat(strict_ok)

    try:
        return run_checks(GatewaySettings(), model_factory=factory, check_headers=False, echo=lambda _: None)
    finally:
        close_model_loop()


def test_all_required_checks_pass_against_a_working_model():
    checks = _run(strict_ok=True)
    assert [c.status for c in checks if c.required] == ["PASS", "PASS", "PASS"]
    assert next(c for c in checks if c.name == "strict json_schema").detail.startswith("supported")


def test_unsupported_strict_schema_is_informational_only():
    checks = _run(strict_ok=False)
    strict = next(c for c in checks if c.name == "strict json_schema")
    assert strict.status == "INFO" and not strict.required and "not supported" in strict.detail
    assert all(c.status == "PASS" for c in checks if c.required)
