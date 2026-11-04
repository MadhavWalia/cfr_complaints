"""Operating procedures as prepared for routing (LDR-0019): redacted sections and the snapshots that list them."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import Field

from ._base import SHA256_PATTERN, Contract
from .artifacts import ArtifactRef


class ProcedureSection(Contract):
    """Redacted text of one section, or one part of a long section. Tables are in Markdown."""

    index: int = Field(ge=0)
    title: str
    part: int = Field(default=1, ge=1)
    page_start: int = Field(ge=1)  # pages of the PDF, as found: contents page numbers can be stale
    page_end: int = Field(ge=1)
    text: str
    pii_masked: int = Field(default=0, ge=0)


class ExtractionQuality(Contract):
    """Numbers only, so they can be shared."""

    pages: int = Field(ge=0)
    method: Literal["contents", "font"]  # how the sections were found
    contents_entries: int = Field(default=0, ge=0)
    contents_located: int = Field(default=0, ge=0)  # entries found as a heading in the body
    tables: int = Field(default=0, ge=0)
    skipped_pages: int = Field(default=0, ge=0)  # the front pages and contents, when contents were found
    oversized_sections: int = Field(default=0, ge=0)  # over the size limit: a single large table


class ProcedureDocument(Contract):
    file_name: str
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    name: str  # redacted; the file name when no name was found on the first pages
    name_from_file: bool = False
    procedure_key: str = Field(min_length=1)  # versions of one procedure share this
    annual_review_date: date | None = None
    monthly_revision_date: date | None = None
    file_date: date | None = None
    extractor_version: str
    redaction_version: str
    quality: ExtractionQuality
    sections: tuple[ProcedureSection, ...]


class ProcedureStatus(StrEnum):
    CURRENT = "CURRENT"  # the version used for routing
    SUPERSEDED = "SUPERSEDED"  # kept, in case an older version is needed later


class VersionFlag(StrEnum):
    """Why someone should check the version chosen."""

    FILE_DATE_USED = "FILE_DATE_USED"  # the front-page dates didn't decide it
    TIED = "TIED"  # nothing separates two different files


class ProcedureSnapshotEntry(Contract):
    file_names: tuple[str, ...] = Field(min_length=1)  # identical files are one entry
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    source_ref: ArtifactRef  # the original PDF, RESTRICTED
    document_ref: ArtifactRef  # the ProcedureDocument, INTERNAL
    procedure_key: str = Field(min_length=1)
    status: ProcedureStatus
    flags: tuple[VersionFlag, ...] = ()


class ProcedureFailure(Contract):
    file_name: str
    error: str  # the exception type only: messages can quote the document


class ProcedureSnapshot(Contract):
    """What was read from one source. No timestamp, so the same input gives the same ref."""

    source: str
    entries: tuple[ProcedureSnapshotEntry, ...]
    failures: tuple[ProcedureFailure, ...] = ()


class ProcedureIndex(Contract):
    """A searchable set of current procedures. Vector rows follow the documents' sections in order;
    a zero row is a section the gateway blocked, which stays findable by keyword."""

    snapshot_ref: ArtifactRef
    embedding_model: str = Field(min_length=1)
    dimensions: int = Field(ge=1)
    vectors_ref: ArtifactRef
    document_refs: tuple[ArtifactRef, ...]


class ProcedureSearchRequest(Contract):
    """``procedure.search``. The query must already be redacted."""

    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=50)
    max_per_document: int = Field(default=3, ge=1)  # so the largest documents can't fill the results
    redacted: bool


class ProcedureHit(Contract):
    """Everything needed to cite the passage, and to review it without re-running the search."""

    procedure_name: str
    file_name: str
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    annual_review_date: date | None = None
    monthly_revision_date: date | None = None
    section_index: int = Field(ge=0)
    section_title: str
    part: int = Field(ge=1)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    text: str
    score: float
    vector_rank: int | None = Field(default=None, ge=1)
    keyword_rank: int | None = Field(default=None, ge=1)


class ProcedureSearchResult(Contract):
    index_ref: ArtifactRef
    vector_search: bool  # false: the query was blocked, so these are keyword results only
    hits: tuple[ProcedureHit, ...]
