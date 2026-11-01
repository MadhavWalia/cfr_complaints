"""Gateway smoke test (LDR-0013): a few real model calls with synthetic text, no case data.

Run on a host with safechain and the ee-config file (CONFIG_PATH), as the worker would:

    python -m cfr_v2.llm.smoke

Checks:

1. the model for CFR_LLM_MODEL_KEY builds from CONFIG_PATH, with safechain redaction disabled,
   and accepts our params (max_retries=0, request_timeout, temperature, max tokens);
2. a plain-text call succeeds and reports token usage;
3. structured output: ``response_format`` (json_schema) reaches the gateway and returns JSON;
4. the same with ``strict`` json_schema (informational: tells you if CFR_LLM_STRICT_SCHEMA=1 works);
5. per-call trace headers (``extra_headers``) reach the HTTP request (needs ee-logging telemetry).

Calls go through ``model_loop()``, the same path stage handlers use. Exit code 0 when no
required check fails.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from cfr_v2.telemetry import elf_headers, init_telemetry, shutdown_telemetry, span

from .bridge import close_model_loop, model_loop
from .client import ModelError, ModelRequest
from .gateway import GatewayModelClient, ModelFactory, safechain_model
from .settings import GatewaySettings

SCHEMA = {
    "title": "SmokeAnswer",
    "type": "object",
    "properties": {"colour": {"type": "string"}, "legs": {"type": "integer"}},
    "required": ["colour", "legs"],
    "additionalProperties": False,
}


@dataclass
class Check:
    name: str
    status: str  # PASS / FAIL / SKIP / INFO
    detail: str = ""
    required: bool = True


def _request(program: str, schema: dict | None) -> ModelRequest:
    return ModelRequest(
        program_id=f"smoke.{program}",
        program_version="1",
        case_id="smoke",
        task_id="smoke",
        instructions=(
            "Answer the question in the input. Reply with the sky's colour on a clear day and how many legs a cat has."
        ),
        inputs={"question": "synthetic smoke test, no case data"},
        redacted=True,
        output_schema=schema,
    )


def run_checks(
    settings: GatewaySettings,
    *,
    model_factory: ModelFactory = safechain_model,
    check_headers: bool = True,
    echo: Callable[[str], None] = print,
) -> list[Check]:
    checks: list[Check] = []
    loop = model_loop()

    def call(client: GatewayModelClient, program: str, schema: dict | None):
        started = time.monotonic()
        response = loop.run(client.complete(_request(program, schema)))
        return response, time.monotonic() - started

    client = GatewayModelClient(settings, model_factory=model_factory)
    echo(f"model key {settings.model_key!r} ({settings.model_id}), params {settings.model_params()}")

    # 1 + 2: build and plain text
    try:
        response, took = call(client, "text", None)
        checks.append(Check("model builds; safechain redaction disabled", "PASS"))
        usage = response.usage
        ok = usage.input_tokens > 0 and usage.output_tokens > 0
        checks.append(
            Check(
                "plain-text call",
                "PASS" if ok else "FAIL",
                f"{took:.1f}s, {usage.input_tokens} in / {usage.output_tokens} out tokens"
                + ("" if ok else " (usage missing)"),
            )
        )
    except ModelError as exc:
        checks.append(Check("model builds; plain-text call", "FAIL", f"{type(exc).__name__}: {exc}"))
        return checks

    # 3: structured output
    try:
        response, took = call(client, "json", SCHEMA)
        ok = isinstance(response.output.get("colour"), str) and isinstance(response.output.get("legs"), int)
        checks.append(
            Check("structured output (json_schema)", "PASS" if ok else "FAIL", f"{took:.1f}s, got {response.output}")
        )
    except ModelError as exc:
        checks.append(Check("structured output (json_schema)", "FAIL", f"{type(exc).__name__}: {exc}"))

    # 4: strict schema (informational)
    strict = GatewayModelClient(dataclasses.replace(settings, strict_schema=True), model_factory=model_factory)
    try:
        response, took = call(strict, "json_strict", SCHEMA)
        checks.append(
            Check("strict json_schema", "INFO", f"supported ({took:.1f}s); CFR_LLM_STRICT_SCHEMA=1 is usable", False)
        )
    except ModelError as exc:
        checks.append(Check("strict json_schema", "INFO", f"not supported ({type(exc).__name__}); keep it off", False))

    # 5: trace headers on the wire
    if check_headers:
        checks.append(_check_trace_headers(client, loop))
    return checks


def _check_trace_headers(client: GatewayModelClient, loop) -> Check:
    name = "trace headers on the request"
    try:
        from cfr_v2.telemetry.http import get_instrumented_httpx2_async_client

        http = get_instrumented_httpx2_async_client()
    except ImportError as exc:  # openai/httpx stack missing: nothing to inspect
        return Check(name, "SKIP", f"no instrumented HTTP client ({type(exc).__name__})", False)

    sent: list[set[str]] = []

    async def record(request) -> None:
        sent.append({k.lower() for k in request.headers})  # names only

    hooks = http.event_hooks
    hooks["request"] = [*hooks.get("request", []), record]
    http.event_hooks = hooks
    try:
        with span("smoke.trace_headers"):
            expected = {k.lower() for k in elf_headers()}
            if not expected:
                return Check(name, "SKIP", "telemetry has no trace headers (ee-logging not active)", False)
            loop.run(client.complete(_request("headers", None)))
    except ModelError as exc:
        return Check(name, "FAIL", f"{type(exc).__name__}: {exc}")
    finally:
        hooks["request"] = [h for h in hooks["request"] if h is not record]
        http.event_hooks = hooks
    if not sent:
        return Check(name, "FAIL", "request did not go through the instrumented client")
    missing = expected - sent[-1]
    return Check(
        name, "FAIL" if missing else "PASS", f"missing {sorted(missing)}" if missing else ", ".join(sorted(expected))
    )


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="python -m cfr_v2.llm.smoke", description=__doc__.split("\n")[0]).parse_args(argv)
    init_telemetry(service_name="cfr-v2-smoke")
    try:
        checks = run_checks(GatewaySettings.from_env())
    finally:
        close_model_loop()
        shutdown_telemetry()
    for c in checks:
        print(f"[{c.status}] {c.name}" + (f": {c.detail}" if c.detail else ""))
    failed = [c for c in checks if c.required and c.status == "FAIL"]
    print("smoke: FAILED" if failed else "smoke: OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
