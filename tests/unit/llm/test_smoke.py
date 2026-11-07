"""The gateway smoke test's checks, run against a fake safechain model (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from cfr_v2.llm import GatewaySettings, close_model_loop, smoke
from cfr_v2.llm.embeddings import EmbeddingSettings
from cfr_v2.llm.smoke import run_checks, run_embedding_load


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


class LoadModel:
    """Stands in for the real embedding model, with a stall on chosen attempts."""

    def __init__(self, *, max_retries=0, request_timeout=120.0, fail_on=()):
        self.max_retries, self.request_timeout = max_retries, request_timeout
        self.redaction = SimpleNamespace(mode="disabled")
        self.fail_on, self.calls = set(fail_on), 0

    def embed_documents(self, batch):
        self.calls += 1
        if self.calls in self.fail_on:
            raise TimeoutError("stalled")
        return [[1.0] * 3 for _ in batch]


def _load(model, **settings):
    async def factory(key, params):
        return model

    config = EmbeddingSettings(dimensions=3, batch_size=64, backoff_base_sec=0.0, backoff_max_sec=0.0, **settings)
    return {c.name: c for c in run_embedding_load(config, factory, rounds=2, texts_per_call=4)}


def test_the_load_check_reports_latency_retries_and_a_timeout():
    checks = _load(LoadModel(fail_on={6}))
    burst = checks["embedding burst: 2 x 5 at once"]
    assert checks["embedding settings"].status == "PASS"
    assert checks["embedding batch: 64 long"].status == "PASS"
    assert burst.status == "PASS" and "1 attempts lost, 1 retries" in burst.detail
    assert "CFR_EMBEDDING_ATTEMPT_TIMEOUT_SEC=30" in checks["suggested attempt timeout"].detail


def test_the_load_check_fails_when_the_model_ignored_our_settings():
    assert _load(LoadModel(max_retries=2))["embedding settings"].status == "FAIL"


def test_the_load_flag_runs_the_load_check(monkeypatch, capsys):
    monkeypatch.setattr(smoke, "run_embedding_load", lambda settings: [smoke.Check("load", "PASS")])
    monkeypatch.setattr(smoke, "init_telemetry", lambda **_: None)
    assert smoke.main(["--embeddings-load"]) == 0
    assert "[PASS] load" in capsys.readouterr().out
