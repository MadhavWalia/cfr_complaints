from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

SHA256_PATTERN = r"^[0-9a-f]{64}$"
ARTIFACT_REF_PATTERN = r"^sha256:[0-9a-f]{64}$"


class Contract(BaseModel):
    """Base for every exchanged contract: strict fields, immutable instances.

    ``SCHEMA_VERSION`` is bumped on breaking changes and recorded alongside
    persisted payloads so old artifacts remain readable.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
        validate_default=True,
    )

    SCHEMA_VERSION: ClassVar[int] = 1
