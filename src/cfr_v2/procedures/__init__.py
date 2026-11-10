"""Operating procedures as routing context (LDR-0019): inspect the PDFs, extract, redact and snapshot them, then
search them with LightRAG."""

from __future__ import annotations

from .corpus import SnapshotResult, build_snapshot, choose_current, load_snapshot
from .extract import ExtractedDocument, extract_pdf
from .inspect import DocumentReport, inspect_pdf, inspect_pdfs
from .search import Passage, ProcedureSearch, corpus_passages
from .sources import (
    ConfluenceSettings,
    ConfluenceSource,
    ManualPdfSource,
    ProcedureSource,
    ProcedureSourceError,
    SourceDocument,
)

__all__ = [
    "ConfluenceSettings",
    "ConfluenceSource",
    "DocumentReport",
    "ExtractedDocument",
    "ManualPdfSource",
    "Passage",
    "ProcedureSearch",
    "ProcedureSource",
    "ProcedureSourceError",
    "SnapshotResult",
    "SourceDocument",
    "build_snapshot",
    "choose_current",
    "corpus_passages",
    "extract_pdf",
    "inspect_pdf",
    "inspect_pdfs",
    "load_snapshot",
]
