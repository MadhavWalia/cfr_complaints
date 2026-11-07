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

``--embeddings`` checks the embedding model instead: it builds, has safechain redaction disabled,
and returns vectors of the expected size for two synthetic texts.

``--embeddings-load`` adds what a build does to it, with synthetic text and in about two minutes:
the model took our retry and timeout settings, batches of real sizes, then bursts at the real
concurrency limit, with latencies, retries and a suggested attempt timeout.

Calls go through ``model_loop()``, the same path stage handlers use. Exit code 0 when no
required check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import math
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from cfr_v2.telemetry import elf_headers, init_telemetry, shutdown_telemetry, span

from .bridge import close_model_loop, model_loop
from .client import ModelError, ModelRequest
from .embeddings import EmbeddingSettings, GatewayEmbeddingClient, safechain_embeddings
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


def run_embedding_check(settings: EmbeddingSettings, factory=safechain_embeddings) -> Check:
    client = GatewayEmbeddingClient(settings, model_factory=factory)
    start = time.monotonic()
    try:
        vectors = model_loop().run(client.embed(["a red bicycle", "a blue boat"], purpose="smoke.embed", redacted=True))
    except ModelError as exc:
        return Check("embeddings", "FAIL", f"{type(exc).__name__}: {exc}")
    return Check("embeddings", "PASS", f"{len(vectors)} vectors of {len(vectors[0])} ({time.monotonic() - start:.1f}s)")


class TimedEmbeddings:
    """The real model, timing every attempt: the client's retries are otherwise out of sight."""

    def __init__(self, model, attempts: list[tuple[float, bool]]) -> None:
        self.model = model
        self.attempts = attempts  # shared: a 401 rebuilds the model

    def __getattr__(self, name: str):
        return getattr(self.model, name)

    def embed_documents(self, batch: list[str]) -> list[list[float]]:
        start = time.monotonic()
        try:
            vectors = self.model.embed_documents(batch)
        except Exception:
            self.attempts.append((time.monotonic() - start, False))
            raise
        self.attempts.append((time.monotonic() - start, True))
        return vectors


def _texts(count: int, chars: int, salt: str) -> list[str]:
    """Synthetic, and different on every call so nothing can be answered from a cache."""
    return [
        (f"{salt} item {i}: the red bicycle crossed the blue bridge. " * (chars // 40 + 1))[:chars]
        for i in range(count)
    ]


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def run_embedding_load(
    settings: EmbeddingSettings, factory=safechain_embeddings, *, rounds: int = 6, texts_per_call: int = 30
) -> list[Check]:
    timed: list[TimedEmbeddings] = []
    attempts: list[tuple[float, bool]] = []

    async def wrapped(key: str, params: dict) -> TimedEmbeddings:
        timed.append(TimedEmbeddings(await factory(key, params), attempts))
        return timed[-1]

    client = GatewayEmbeddingClient(settings, model_factory=wrapped)

    async def call(texts: list[str]) -> float:
        start = time.monotonic()
        await client.embed(texts, purpose="smoke.embed.load", redacted=True)
        return time.monotonic() - start

    async def run() -> list[Check]:
        checks = []
        try:
            await call(_texts(1, 20, "warm"))
        except ModelError as exc:
            return [Check("embedding load", "FAIL", f"{type(exc).__name__}: {exc}")]
        model = timed[-1]
        retries, timeout = getattr(model, "max_retries", None), getattr(model, "request_timeout", None)
        applied = retries == 0 and timeout == settings.attempt_timeout_sec
        detail = f"max_retries={retries}, request_timeout={timeout}"
        checks.append(Check("embedding settings", "PASS" if applied else "FAIL", detail))

        for name, count, chars in (
            ("8 short", 8, 300),
            ("64 short", settings.batch_size, 300),
            ("64 long", settings.batch_size, 6000),
        ):
            try:
                seconds = await call(_texts(count, chars, name))
                checks.append(Check(f"embedding batch: {name}", "PASS", f"{seconds:.1f}s"))
            except ModelError as exc:
                checks.append(Check(f"embedding batch: {name}", "FAIL", type(exc).__name__))

        attempts.clear()
        size, failures, latencies = settings.max_concurrency, 0, []
        for number in range(rounds):
            burst = [call(_texts(texts_per_call, 300, f"burst {number} {i}")) for i in range(size)]
            for result in await asyncio.gather(*burst, return_exceptions=True):
                if isinstance(result, ModelError):
                    failures += 1
                elif isinstance(result, BaseException):
                    raise result
                else:
                    latencies.append(result)
        extra, lost = len(attempts) - rounds * size, sum(not ok for _, ok in attempts)
        name = f"embedding burst: {rounds} x {size} at once"
        if not latencies:
            return [*checks, Check(name, "FAIL", f"all {failures} calls failed")]
        p95 = _percentile(latencies, 0.95)
        detail = (
            f"median {statistics.median(latencies):.1f}s, p95 {p95:.1f}s, slowest {max(latencies):.1f}s; "
            f"{failures} failed, {lost} attempts lost, {extra} retries"
        )
        checks.append(Check(name, "FAIL" if failures else "PASS", detail))
        good = [seconds for seconds, ok in attempts if ok]
        if good:
            suggested = max(30, math.ceil(3 * _percentile(good, 0.95) / 5) * 5)
            hint = f"CFR_EMBEDDING_ATTEMPT_TIMEOUT_SEC={suggested} (3 x p95 of good attempts, at least 30)"
            checks.append(Check("suggested attempt timeout", "INFO", hint, required=False))
        return checks

    return model_loop().run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cfr_v2.llm.smoke", description=__doc__.split("\n")[0])
    parser.add_argument("--embeddings", action="store_true", help="check the embedding model instead")
    parser.add_argument("--embeddings-load", action="store_true", help="measure the embedding model at a build's load")
    args = parser.parse_args(argv)
    init_telemetry(service_name="cfr-v2-smoke")
    try:
        if args.embeddings_load:
            checks = run_embedding_load(EmbeddingSettings.from_env())
        elif args.embeddings:
            checks = [run_embedding_check(EmbeddingSettings.from_env())]
        else:
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
