"""Gateway client settings (LDR-0013), read from ``CFR_LLM_*`` environment variables.

Which model a key names (deployment, API version, IDaaS scope) lives in the
``models:`` section of the ee-config file (``CONFIG_PATH``), read by safechain.
Everything here is about how hard this process may push that model.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GatewaySettings:
    # Key under `models:` in config_gpt.yml ("3" = gpt-5 in V1) and the name reported in usage.
    model_key: str = "3"
    model_id: str = "gpt-5"
    max_completion_tokens: int = 16384
    temperature: float = 0.0

    # Admission, shared by every case in the worker process.
    max_concurrency: int = 32
    # Optional steady pacing on top of the concurrency cap; None = no pacing.
    requests_per_minute: float | None = None

    # Retries inside one call. 429s don't count as attempts; the deadline bounds them.
    max_attempts: int = 4
    backoff_base_sec: float = 2.0
    backoff_max_sec: float = 30.0
    attempt_timeout_sec: float = 180.0
    call_deadline_sec: float = 600.0
    # Schema-invalid responses tolerated before the call fails permanently.
    invalid_output_attempts: int = 2
    # Ask Azure to enforce output_schema exactly (json_schema "strict"). Needs every property
    # listed in `required` and `additionalProperties: false` at each level, or the call is rejected.
    strict_schema: bool = False

    # A 429 pauses every call in the process: base doubling per strike, capped, never
    # shorter than the gateway's Retry-After.
    rate_limit_cooldown_sec: float = 5.0
    rate_limit_cooldown_max_sec: float = 60.0

    def model_params(self) -> dict[str, Any]:
        """Constructor args for the safechain model (merged into its config ``model_kwargs``)."""
        return {
            "max_completion_tokens": self.max_completion_tokens,
            "temperature": self.temperature,
            # Retries and timeouts are this client's job; the openai SDK would otherwise retry
            # twice more inside every attempt, unseen by the rate gate.
            "max_retries": 0,
            "request_timeout": self.attempt_timeout_sec,
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> GatewaySettings:
        d = cls()
        rpm = env.get("CFR_LLM_REQUESTS_PER_MINUTE")
        return cls(
            model_key=env.get("CFR_LLM_MODEL_KEY", d.model_key),
            model_id=env.get("CFR_LLM_MODEL_ID", d.model_id),
            max_completion_tokens=int(env.get("CFR_LLM_MAX_COMPLETION_TOKENS", d.max_completion_tokens)),
            temperature=float(env.get("CFR_LLM_TEMPERATURE", d.temperature)),
            max_concurrency=int(env.get("CFR_LLM_MAX_CONCURRENCY", d.max_concurrency)),
            requests_per_minute=float(rpm) if rpm else None,
            max_attempts=int(env.get("CFR_LLM_MAX_ATTEMPTS", d.max_attempts)),
            backoff_base_sec=float(env.get("CFR_LLM_BACKOFF_BASE_SEC", d.backoff_base_sec)),
            backoff_max_sec=float(env.get("CFR_LLM_BACKOFF_MAX_SEC", d.backoff_max_sec)),
            attempt_timeout_sec=float(env.get("CFR_LLM_ATTEMPT_TIMEOUT_SEC", d.attempt_timeout_sec)),
            call_deadline_sec=float(env.get("CFR_LLM_CALL_DEADLINE_SEC", d.call_deadline_sec)),
            invalid_output_attempts=int(env.get("CFR_LLM_INVALID_OUTPUT_ATTEMPTS", d.invalid_output_attempts)),
            strict_schema=env.get("CFR_LLM_STRICT_SCHEMA", str(d.strict_schema)).lower() in ("1", "true", "yes"),
            rate_limit_cooldown_sec=float(env.get("CFR_LLM_RATE_LIMIT_COOLDOWN_SEC", d.rate_limit_cooldown_sec)),
            rate_limit_cooldown_max_sec=float(
                env.get("CFR_LLM_RATE_LIMIT_COOLDOWN_MAX_SEC", d.rate_limit_cooldown_max_sec)
            ),
        )
