"""P5 — model gateway client. See lld/modules/p5-llm-gateway/README.md and LDR-0013."""

from __future__ import annotations

from .bridge import ModelLoop, close_model_loop, model_loop
from .client import (
    BudgetExhaustedError,
    BudgetTracker,
    GuardedModelClient,
    ModelBlockedError,
    ModelClient,
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
    UnredactedPayloadError,
)
from .fake import FAKE_MODEL_ID, FakeModelClient
from .gateway import Classified, Failure, GatewayModelClient, classify, safechain_model
from .limiter import RateGate
from .settings import GatewaySettings

__all__ = [
    "FAKE_MODEL_ID",
    "BudgetExhaustedError",
    "BudgetTracker",
    "Classified",
    "Failure",
    "FakeModelClient",
    "GatewayModelClient",
    "GatewaySettings",
    "GuardedModelClient",
    "ModelBlockedError",
    "ModelClient",
    "ModelConfigError",
    "ModelError",
    "ModelLoop",
    "ModelOutputInvalidError",
    "ModelRateLimitedError",
    "ModelRequest",
    "ModelResponse",
    "ModelTimeoutError",
    "ModelUsage",
    "PermanentModelError",
    "RateGate",
    "RetryableModelError",
    "UnredactedPayloadError",
    "classify",
    "close_model_loop",
    "model_loop",
    "safechain_model",
]
