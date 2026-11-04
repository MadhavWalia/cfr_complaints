"""Operating procedures as routing context (LDR-0019). ``inspect`` reports on the PDFs; the rest is to come."""

from __future__ import annotations

from .inspect import DocumentReport, inspect_pdf, inspect_pdfs

__all__ = ["DocumentReport", "inspect_pdf", "inspect_pdfs"]
