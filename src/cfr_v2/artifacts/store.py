"""P3 content-addressed artifact store: bytes in S3, metadata in Postgres (LDR-0007).

Write protocol: ``put`` (idempotent upload) first, then ``register`` inside the
caller's case-state transaction. A crash in between leaves an orphan object,
never a dangling reference.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from botocore.exceptions import ClientError

from cfr_v2.contracts import (
    ArtifactMetadata,
    AuthorizationContext,
    Classification,
    ref_from_sha256,
    sha256_from_ref,
    sha256_hex,
)
from cfr_v2.state import CaseStateRepository
from cfr_v2.telemetry import get_logger

from .s3 import ReconnectingClient, S3Settings, make_client

logger = get_logger(__name__)

DEFAULT_MAX_BYTES = 10 * 1024 * 1024


class ArtifactError(RuntimeError):
    pass


class ArtifactTooLargeError(ArtifactError):
    pass


class ArtifactIntegrityError(ArtifactError):
    pass


class ArtifactAccessDeniedError(ArtifactError):
    pass


class ArtifactNotFoundError(ArtifactError):
    pass


@dataclass(frozen=True)
class StoredArtifact:
    metadata: ArtifactMetadata
    bucket: str
    key: str


def object_key(digest: str) -> str:
    return f"artifacts/sha256/{digest[0:2]}/{digest[2:4]}/{digest}"


class ArtifactStore(Protocol):
    def put(self, data: bytes, *, media_type: str, classification: Classification) -> StoredArtifact: ...

    def get(self, ref: str, *, classification: Classification, authorization: AuthorizationContext) -> bytes: ...


def register(repo: CaseStateRepository, stored: StoredArtifact) -> None:
    """Record metadata in the caller's transaction (step 2 of the write protocol)."""
    repo.register_artifact(stored.metadata, bucket=stored.bucket, key=stored.key)


def check_access(classification: Classification, authorization: AuthorizationContext) -> None:
    if classification.value not in authorization.allowed_classifications:
        raise ArtifactAccessDeniedError(
            f"{authorization.principal} may not read {classification.value} artifacts for {authorization.purpose}"
        )


class S3ArtifactStore:
    def __init__(
        self,
        settings: S3Settings,
        *,
        clients: dict[Classification, ReconnectingClient] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.settings = settings
        self.max_bytes = max_bytes
        self._clients = clients or {
            c: ReconnectingClient(lambda c=c: make_client(settings, settings.credentials_for(c)))
            for c in Classification
        }
        if settings.restricted_credentials is None:
            logger.warning(
                "artifact_store: no separate restricted credentials; RESTRICTED/INTERNAL separation is logical only"
            )

    def put(self, data: bytes, *, media_type: str, classification: Classification) -> StoredArtifact:
        if len(data) > self.max_bytes:
            raise ArtifactTooLargeError(f"{len(data)} bytes exceeds limit of {self.max_bytes}")
        digest = sha256_hex(data)
        bucket, key = self.settings.bucket_for(classification), object_key(digest)
        try:
            self._clients[classification].call(
                "put_object",
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=media_type,
                IfNoneMatch="*",
            )
            logger.debug("artifact_put bucket=%s key=%s bytes=%d", bucket, key, len(data))
        except ClientError as exc:
            # Content-addressed: an existing object at this key holds identical bytes.
            if _error_code(exc) not in ("PreconditionFailed", "ConditionalRequestConflict", "412"):
                raise
            logger.debug("artifact_put_exists bucket=%s key=%s", bucket, key)
        meta = ArtifactMetadata(
            ref=ref_from_sha256(digest), media_type=media_type, classification=classification, size_bytes=len(data)
        )
        return StoredArtifact(meta, bucket, key)

    def get(self, ref: str, *, classification: Classification, authorization: AuthorizationContext) -> bytes:
        check_access(classification, authorization)
        digest = sha256_from_ref(ref)
        bucket, key = self.settings.bucket_for(classification), object_key(digest)
        try:
            body = self._clients[classification].call("get_object", Bucket=bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if _error_code(exc) in ("NoSuchKey", "404"):
                raise ArtifactNotFoundError(ref) from exc
            raise
        if sha256_hex(body) != digest:
            raise ArtifactIntegrityError(f"content of {ref} does not match its hash")
        return body

    async def aput(self, data: bytes, *, media_type: str, classification: Classification) -> StoredArtifact:
        return await asyncio.to_thread(self.put, data, media_type=media_type, classification=classification)

    async def aget(self, ref: str, *, classification: Classification, authorization: AuthorizationContext) -> bytes:
        return await asyncio.to_thread(self.get, ref, classification=classification, authorization=authorization)

    def ensure_buckets(self) -> None:
        """Dev/test provisioning only; production buckets come from platform (OQ-18)."""
        for classification in Classification:
            bucket = self.settings.bucket_for(classification)
            client = self._clients[classification]
            try:
                client.call("head_bucket", Bucket=bucket)
            except ClientError:
                client.call("create_bucket", Bucket=bucket)


def _error_code(exc: ClientError) -> str:
    err: dict[str, Any] = exc.response.get("Error", {})
    return str(err.get("Code", ""))
