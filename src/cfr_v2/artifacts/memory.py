"""In-memory artifact store for unit tests and offline development."""
from __future__ import annotations

import threading

from cfr_v2.contracts import (
    ArtifactMetadata,
    AuthorizationContext,
    Classification,
    ref_from_sha256,
    sha256_from_ref,
    sha256_hex,
)

from .store import DEFAULT_MAX_BYTES, ArtifactNotFoundError, ArtifactTooLargeError, StoredArtifact, check_access, object_key


class InMemoryArtifactStore:
    """Same contract as ``S3ArtifactStore``: content-addressed, write-once, access-checked reads."""

    def __init__(self, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.max_bytes = max_bytes
        self.objects: dict[tuple[Classification, str], bytes] = {}
        self._lock = threading.Lock()

    def put(self, data: bytes, *, media_type: str, classification: Classification) -> StoredArtifact:
        if len(data) > self.max_bytes:
            raise ArtifactTooLargeError(f"{len(data)} bytes exceeds limit of {self.max_bytes}")
        digest = sha256_hex(data)
        with self._lock:
            self.objects.setdefault((classification, digest), data)
        meta = ArtifactMetadata(
            ref=ref_from_sha256(digest), media_type=media_type, classification=classification, size_bytes=len(data)
        )
        return StoredArtifact(meta, f"memory-{classification.value.lower()}", object_key(digest))

    def get(self, ref: str, *, classification: Classification, authorization: AuthorizationContext) -> bytes:
        check_access(classification, authorization)
        try:
            return self.objects[(classification, sha256_from_ref(ref))]
        except KeyError:
            raise ArtifactNotFoundError(ref) from None
