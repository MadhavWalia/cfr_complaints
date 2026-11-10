"""P4 registry entries and the release manifest pinned at admission (ADR-14)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, JsonValue, ValidationInfo, model_validator

from ._base import SHA256_PATTERN, Contract
from .artifacts import ArtifactRef
from .hashing import canonical_hash

_UNCHECKED = "cfr_unchecked_checksum"  # validation context flag used only by ReleaseManifest.build


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


SOURCE_TYPE_ID_PATTERN = r"^[a-z][a-z0-9_]*$"


class BackendStatus(StrEnum):
    AVAILABLE = "available"  # the source exists and can be queried
    PLANNED = "planned"  # agreed, not yet connected
    NONE = "none"  # no known source yet; the need is still recorded


class EvidenceBackend(Contract):
    """Where evidence of one source type lives. Not model-visible; C05/C07 configuration later."""

    system: str = Field(min_length=1)  # e.g. "LUMI (BigQuery)", "procedure documents", "unknown"
    dataset: str | None = None  # table or index name; never a query
    status: BackendStatus
    owner: str | None = None
    notes: str | None = None


class EvidenceSourceType(Contract):
    """One kind of evidence Pass 2 can ask for. ``id``, ``description`` and ``examples`` reach the model."""

    id: str = Field(pattern=SOURCE_TYPE_ID_PATTERN)
    description: str = Field(min_length=1)
    examples: tuple[str, ...] = ()
    deprecated: bool = False
    replaced_by: str | None = None
    backends: tuple[EvidenceBackend, ...] = Field(min_length=1)  # a "none" row makes a gap visible

    @model_validator(mode="after")
    def _replacement_only_when_deprecated(self) -> EvidenceSourceType:
        if self.replaced_by is not None and not self.deprecated:
            raise ValueError(f"{self.id}: replaced_by given for a source type that is not deprecated")
        return self


class EvidenceSourceTaxonomy(Contract):
    """The evidence source types and their backend mapping (P4 registry, ``registry/evidence_sources``).

    The model-facing part (ids, descriptions, examples, deprecations) is fixed per version:
    ``taxonomy_hash`` covers it and claim sets record it. Backends can change within a version.
    """

    version: str = Field(min_length=1)
    source_types: tuple[EvidenceSourceType, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> EvidenceSourceTaxonomy:
        ids = [t.id for t in self.source_types]
        if len(ids) != len(set(ids)):
            raise ValueError("source type ids must be unique")
        active = {t.id for t in self.source_types if not t.deprecated}
        if not active:
            raise ValueError("at least one source type must be active")
        for t in self.source_types:
            if t.replaced_by is not None and t.replaced_by not in active:
                raise ValueError(f"{t.id}: replaced_by {t.replaced_by!r} is not an active source type")
        return self

    def active(self) -> tuple[EvidenceSourceType, ...]:
        return tuple(t for t in self.source_types if not t.deprecated)

    @property
    def taxonomy_hash(self) -> str:
        return canonical_hash(
            [
                t.model_dump(mode="json", include={"id", "description", "examples", "deprecated", "replaced_by"})
                for t in self.source_types
            ]
        )


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


# Left out of the checksum when unset, so manifests pinned before each existed keep their checksum.
_OPTIONAL_PINS = ("evidence_sources", "routing_verticals", "procedure_snapshot")


class ReleaseManifest(Contract):
    release_id: str = Field(min_length=1)
    programs: tuple[ProgramRef, ...] = ()
    skills: tuple[SkillPin, ...] = ()
    capabilities: tuple[str, ...] = ()  # "<capability_id>@<version>"
    router_policy_version: str | None = None
    decision_policy_version: str | None = None
    # Evidence source taxonomy version for C02 Pass 2 (registry/evidence_sources/<version>.yaml);
    # None: the default version.
    evidence_sources: str | None = None
    # C04 (LDR-0021): the knowledge document defining the verticals, stored as an artifact, and
    # the procedure snapshot the LightRAG store was built from. None: the release can't route.
    routing_verticals: ArtifactRef | None = None
    procedure_snapshot: ArtifactRef | None = None
    checksum: str = Field(pattern=SHA256_PATTERN)

    def _checksum_body(self) -> dict[str, Any]:
        body = self.model_dump(mode="json", exclude={"checksum"})
        for optional in _OPTIONAL_PINS:
            if body[optional] is None:
                del body[optional]
        return body

    @classmethod
    def build(cls, **body: Any) -> "ReleaseManifest":
        """Construct a manifest, computing its checksum from the validated content."""
        draft = cls.model_validate({**body, "checksum": "0" * 64}, context={_UNCHECKED: True})
        return cls(**body, checksum=canonical_hash(draft._checksum_body()))

    @model_validator(mode="after")
    def _checksum_matches(self, info: ValidationInfo) -> "ReleaseManifest":
        if info.context and info.context.get(_UNCHECKED):
            return self
        if self.checksum != canonical_hash(self._checksum_body()):
            raise ValueError("manifest checksum does not match content")
        return self

    @property
    def ref(self) -> str:
        return f"{self.release_id}@{self.checksum[:12]}"
