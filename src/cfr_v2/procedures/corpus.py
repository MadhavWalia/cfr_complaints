"""Turn a procedure source into a snapshot (LDR-0019): extract, redact, store, pick the current versions.

Originals are stored as RESTRICTED. Only redacted text is stored as INTERNAL, and only that goes on
to indexing. A document that can't be read or redacted is reported and left out, never stored unredacted.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

from cfr_v2.artifacts import StoredArtifact
from cfr_v2.contracts import (
    AuthorizationContext,
    Classification,
    ExtractionQuality,
    ProcedureDocument,
    ProcedureFailure,
    ProcedureSection,
    ProcedureSnapshot,
    ProcedureSnapshotEntry,
    ProcedureStatus,
    VersionFlag,
    canonical_json,
    ref_from_sha256,
)
from cfr_v2.intake.redaction import REDACTION_VERSION, Masker, ProfanityFilter, redact

from .extract import EXTRACTOR_VERSION, ExtractedDocument, date_from_file_name, extract_pdf, procedure_key
from .sources import ProcedureSource, SourceDocument

_READER = AuthorizationContext(principal="procedures.snapshot", purpose="reuse earlier extraction")
_NO_WORDS = ProfanityFilter()  # reference documents: a word list would only remove terms they need


def redaction_version(masker: Masker) -> str:
    return f"{REDACTION_VERSION}+{masker.version}"


def redact_document(
    extracted: ExtractedDocument, *, file_name: str, modified: date | None, sha256: str, masker: Masker
) -> ProcedureDocument:
    def clean(text: str, label: str):
        return redact(text, interaction_id=f"{sha256}:{label}", masker=masker, profanity=_NO_WORDS)

    sections = []
    for index, section in enumerate(extracted.sections):
        title, text = clean(section.title, f"{index}:title"), clean(section.text, str(index))
        sections.append(
            ProcedureSection(
                index=index,
                title=title.text,
                part=section.part,
                page_start=section.page_start,
                page_end=section.page_end,
                text=text.text,
                pii_masked=len(text.map.spans) + len(title.map.spans),
            )
        )
    name = clean(extracted.name, "name").text if extracted.name else file_name
    return ProcedureDocument(
        file_name=file_name,
        source_sha256=sha256,
        name=name,
        name_from_file=extracted.name is None,
        procedure_key=procedure_key(name) or procedure_key(file_name) or sha256,
        annual_review_date=extracted.annual_review_date,
        monthly_revision_date=extracted.monthly_revision_date,
        file_date=date_from_file_name(file_name) or modified,
        extractor_version=EXTRACTOR_VERSION,
        redaction_version=redaction_version(masker),
        quality=ExtractionQuality(
            pages=extracted.pages,
            method=extracted.method,
            contents_entries=extracted.contents_entries,
            contents_located=extracted.contents_located,
            tables=extracted.tables,
            skipped_pages=extracted.skipped_pages,
            oversized_sections=extracted.oversized_sections,
        ),
        sections=tuple(sections),
    )


def _rank(document: ProcedureDocument) -> tuple[date, date, date]:
    return (
        document.monthly_revision_date or date.min,
        document.annual_review_date or date.min,
        document.file_date or date.min,
    )


def choose_current(versions: Sequence[ProcedureDocument]) -> tuple[ProcedureDocument, tuple[VersionFlag, ...]]:
    """Latest monthly revision date, then annual review date, then the file's date. Anything
    decided by the file's date, or by nothing at all, is flagged for review."""
    ordered = sorted(versions, key=lambda d: (*_rank(d), d.file_name), reverse=True)
    best = ordered[0]
    flags = []
    runner_up = ordered[1] if len(ordered) > 1 else None
    if runner_up and _rank(runner_up) == _rank(best):
        flags.append(VersionFlag.TIED)
    elif (best.monthly_revision_date is None and best.annual_review_date is None) or (
        runner_up and _rank(runner_up)[:2] == _rank(best)[:2]
    ):
        flags.append(VersionFlag.FILE_DATE_USED)
    return best, tuple(flags)


@dataclass
class SnapshotResult:
    ref: str
    snapshot: ProcedureSnapshot
    documents: dict[str, ProcedureDocument]  # by source_sha256
    stored: list[StoredArtifact] = field(default_factory=list)  # to register
    reused: int = 0  # documents taken from the previous snapshot unchanged


def _earlier(store, previous: ProcedureSnapshot | None, masker: Masker) -> dict[str, tuple[str, ProcedureDocument]]:
    """Reusable documents by hash. A new extractor or masker means reading them again."""
    reusable: dict[str, tuple[str, ProcedureDocument]] = {}
    for entry in previous.entries if previous else ():
        raw = store.get(entry.document_ref, classification=Classification.INTERNAL, authorization=_READER)
        document = ProcedureDocument.model_validate_json(raw)
        if document.extractor_version == EXTRACTOR_VERSION and document.redaction_version == redaction_version(masker):
            reusable[entry.source_sha256] = (entry.document_ref, document)
    return reusable


def load_snapshot(store, ref: str) -> ProcedureSnapshot:
    raw = store.get(ref, classification=Classification.INTERNAL, authorization=_READER)
    return ProcedureSnapshot.model_validate_json(raw)


def build_snapshot(
    source: ProcedureSource,
    *,
    store,
    masker: Masker,
    previous: ProcedureSnapshot | None = None,
    progress: Callable[[int, int, SourceDocument], None] | None = None,
) -> SnapshotResult:
    reusable = _earlier(store, previous, masker)
    documents: dict[str, ProcedureDocument] = {}
    refs: dict[str, tuple[str, str]] = {}  # hash -> (original, document)
    names: dict[str, list[str]] = defaultdict(list)
    failures: list[ProcedureFailure] = []
    stored: list[StoredArtifact] = []
    reused = 0
    listed = source.list_documents()
    for number, item in enumerate(listed, start=1):
        if progress:
            progress(number, len(listed), item)
        try:
            data = source.fetch(item)
            sha256 = hashlib.sha256(data).hexdigest()
            names[sha256].append(item.name)
            if sha256 in documents:
                continue
            if sha256 in reusable:
                refs[sha256] = (ref_from_sha256(sha256), reusable[sha256][0])
                documents[sha256] = reusable[sha256][1]
                reused += 1
                continue
            document = redact_document(
                extract_pdf(data), file_name=item.name, modified=item.modified, sha256=sha256, masker=masker
            )
            original = store.put(data, media_type="application/pdf", classification=Classification.RESTRICTED)
            prepared = store.put(
                canonical_json(document), media_type="application/json", classification=Classification.INTERNAL
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest; the type carries no content
            failures.append(ProcedureFailure(file_name=item.name, error=type(exc).__name__))
            continue
        stored += [original, prepared]
        refs[sha256] = (original.metadata.ref, prepared.metadata.ref)
        documents[sha256] = document

    groups: dict[str, list[ProcedureDocument]] = defaultdict(list)
    for document in documents.values():
        groups[document.procedure_key].append(document)
    chosen = {key: choose_current(versions) for key, versions in groups.items()}
    entries = []
    for sha256, document in sorted(documents.items(), key=lambda item: (item[1].procedure_key, item[0])):
        best, flags = chosen[document.procedure_key]
        current = best.source_sha256 == sha256
        entries.append(
            ProcedureSnapshotEntry(
                file_names=tuple(sorted(names[sha256])),
                source_sha256=sha256,
                source_ref=refs[sha256][0],
                document_ref=refs[sha256][1],
                procedure_key=document.procedure_key,
                status=ProcedureStatus.CURRENT if current else ProcedureStatus.SUPERSEDED,
                flags=flags if current else (),
            )
        )
    snapshot = ProcedureSnapshot(source=source.name, entries=tuple(entries), failures=tuple(failures))
    saved = store.put(canonical_json(snapshot), media_type="application/json", classification=Classification.INTERNAL)
    return SnapshotResult(saved.metadata.ref, snapshot, documents, [*stored, saved], reused)
