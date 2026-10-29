from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from ._base import ARTIFACT_REF_PATTERN, Contract

ArtifactRef = Annotated[str, StringConstraints(pattern=ARTIFACT_REF_PATTERN)]
"""Content address of an immutable artifact: ``sha256:<64 hex>``."""


class Classification(StrEnum):
    RESTRICTED = "RESTRICTED"  # original transcript, redaction map
    INTERNAL = "INTERNAL"  # redacted text, model outputs, snapshots


class ArtifactMetadata(Contract):
    ref: ArtifactRef
    media_type: str = Field(min_length=1)
    classification: Classification
    size_bytes: int = Field(ge=0)


def ref_from_sha256(digest: str) -> str:
    return f"sha256:{digest}"


def sha256_from_ref(ref: str) -> str:
    prefix, _, digest = ref.partition(":")
    if prefix != "sha256" or len(digest) != 64:
        raise ValueError(f"not an artifact ref: {ref!r}")
    return digest
