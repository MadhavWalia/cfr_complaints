"""Operating procedures as routing context (LDR-0019): inspect the PDFs, then extract, redact and snapshot them."""

from __future__ import annotations

from .corpus import SnapshotResult, build_snapshot, choose_current, load_snapshot
from .extract import ExtractedDocument, extract_pdf
from .inspect import DocumentReport, inspect_pdf, inspect_pdfs
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
    "ProcedureSource",
    "ProcedureSourceError",
    "SnapshotResult",
    "SourceDocument",
    "build_snapshot",
    "choose_current",
    "extract_pdf",
    "inspect_pdf",
    "inspect_pdfs",
    "load_snapshot",
]
