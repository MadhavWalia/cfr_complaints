"""P4 registry entries and the release manifest pinned at admission (ADR-14)."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, JsonValue, model_validator

from ._base import SHA256_PATTERN, Contract
from .artifacts import ArtifactRef
from .hashing import canonical_hash


class EvidenceRequirementClass(StrEnum):
    HARD = "hard"
    EXPECTED = "expected"
    PREFERRED = "preferred"


class EvidenceRequirement(Contract):
    capability_id: str = Field(min_length=1)
    requirement_class: EvidenceRequirementClass
    condition: str | None = None  # approved predicate; None = unconditional


class VerticalSkill(Contract):
    vertical_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    principle: str = Field(min_length=1)
    archetypal_failures: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    routing_keywords: tuple[str, ...] = ()
    evidence_requirements: tuple[EvidenceRequirement, ...] = ()  # empty until OQ-07
    activatable: bool = True  # False for UNMAPPED (HD-18)


class CostClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CapabilityDescriptor(Contract):
    capability_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameter_schema: dict[str, JsonValue] = Field(default_factory=dict)
    allowed_purposes: tuple[str, ...] = Field(min_length=1)
    cost_class: CostClass = CostClass.MEDIUM


class ProgramRef(Contract):
    program_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    module: str = Field(min_length=1)  # import path of the DSPy program
    optimized_state_ref: ArtifactRef | None = None
    model_config_ref: str | None = None


class SkillPin(Contract):
    vertical_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    program_id: str | None = None


class ReleaseManifest(Contract):
    release_id: str = Field(min_length=1)
    programs: tuple[ProgramRef, ...] = ()
    skills: tuple[SkillPin, ...] = ()
    capabilities: tuple[str, ...] = ()  # "<capability_id>@<version>"
    router_policy_version: str | None = None
    decision_policy_version: str | None = None
    checksum: str = Field(pattern=SHA256_PATTERN)

    @classmethod
    def build(cls, **body: Any) -> "ReleaseManifest":
        """Construct a manifest, computing its checksum from the content."""
        unchecked = cls.model_construct(**body, checksum="0" * 64)
        checksum = canonical_hash(unchecked.model_dump(mode="json", exclude={"checksum"}))
        return cls(**body, checksum=checksum)

    @model_validator(mode="after")
    def _checksum_matches(self) -> "ReleaseManifest":
        if self.checksum != canonical_hash(self.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError("manifest checksum does not match content")
        return self

    @property
    def ref(self) -> str:
        return f"{self.release_id}@{self.checksum[:12]}"
