"""S3 client construction following the V1 access pattern (cfr-v1/file.txt, LDR-0008).

Differences from V1: TLS verification is always on (CA bundle or system trust
store) and botocore's standard retry mode handles throttling and 5xx.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

import boto3
from botocore.client import Config
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError

from cfr_v2.contracts import Classification
from cfr_v2.telemetry import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

MAX_RECONNECTS = 3
RECONNECT_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class S3Credentials:
    access_key: str
    secret_key: str


@dataclass(frozen=True)
class S3Settings:
    endpoint_url: str | None
    region_name: str | None
    credentials: S3Credentials
    restricted_credentials: S3Credentials | None
    ca_bundle: str | None
    bucket: str
    restricted_bucket: str
    # botocore defaults to 10; size at or above the runtime's concurrent workflow slots.
    max_pool_connections: int = 64

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "S3Settings":
        cfr_env = env.get("CFR_ENV", "dev")
        restricted = None
        if env.get("S3_RESTRICTED_ACCESSKEY") and env.get("S3_RESTRICTED_SECRET"):
            restricted = S3Credentials(env["S3_RESTRICTED_ACCESSKEY"], env["S3_RESTRICTED_SECRET"])
        return cls(
            endpoint_url=env.get("S3_ENDPOINT") or None,
            region_name=env.get("REGION_NAME") or None,
            credentials=S3Credentials(env["S3_ACCESSKEY"], env["S3_SECRET"]),
            restricted_credentials=restricted,
            ca_bundle=env.get("S3_CA_BUNDLE") or None,
            bucket=env.get("CFR_ARTIFACT_BUCKET") or f"cfr-v2-{cfr_env}-artifacts",
            restricted_bucket=env.get("CFR_RESTRICTED_ARTIFACT_BUCKET") or f"cfr-v2-{cfr_env}-artifacts-restricted",
            max_pool_connections=int(env.get("S3_MAX_POOL_CONNECTIONS", cls.max_pool_connections)),
        )

    def bucket_for(self, classification: Classification) -> str:
        return self.restricted_bucket if classification is Classification.RESTRICTED else self.bucket

    def credentials_for(self, classification: Classification) -> S3Credentials:
        if classification is Classification.RESTRICTED and self.restricted_credentials is not None:
            return self.restricted_credentials
        return self.credentials


def client_config(max_pool_connections: int = S3Settings.max_pool_connections) -> Config:
    return Config(
        s3={"addressing_style": "path"},
        signature_version="s3v4",
        retries={"mode": "standard", "max_attempts": 5},
        max_pool_connections=max_pool_connections,
    )


def make_client(settings: S3Settings, credentials: S3Credentials) -> Any:
    return boto3.client(
        "s3",
        region_name=settings.region_name,
        aws_access_key_id=credentials.access_key,
        aws_secret_access_key=credentials.secret_key,
        endpoint_url=settings.endpoint_url,
        config=client_config(settings.max_pool_connections),
        # Never False: a CA bundle path, or True for the system trust store (LDR-0008).
        verify=settings.ca_bundle or True,
    )


class ReconnectingClient:
    """V1 pattern: rebuild the client and retry when the connection drops."""

    def __init__(self, factory: Callable[[], Any], *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._factory = factory
        self._sleep = sleep
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def call(self, operation: str, **kwargs: Any) -> Any:
        for attempt in range(1, MAX_RECONNECTS + 1):
            try:
                return getattr(self.client, operation)(**kwargs)
            except (ConnectionClosedError, EndpointConnectionError) as exc:
                logger.warning("s3_connection_error op=%s attempt=%d/%d error=%s", operation, attempt, MAX_RECONNECTS, exc)
                if attempt == MAX_RECONNECTS:
                    raise
                self._sleep(RECONNECT_DELAY_SECONDS * attempt)
                self._client = None
        raise AssertionError("unreachable")  # pragma: no cover
