"""Where procedure PDFs come from (LDR-0019): one pipeline, two sources.

``ManualPdfSource`` reads a folder of downloaded PDFs. ``ConfluenceSource`` is a stub until API
access is granted (OQ-30). The site's host name stays in configuration: this repo is public.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol


class ProcedureSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceDocument:
    name: str  # a file name
    locator: str  # how the source finds it again
    modified: date | None = None


class ProcedureSource(Protocol):
    name: str

    def list_documents(self) -> Sequence[SourceDocument]: ...

    def fetch(self, document: SourceDocument) -> bytes: ...


class ManualPdfSource:
    """A folder of PDFs, downloaded by hand."""

    name = "manual"

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def list_documents(self) -> list[SourceDocument]:
        if not self.folder.is_dir():
            raise ProcedureSourceError(f"not a folder: {self.folder}")
        return [
            SourceDocument(
                path.name, str(path), datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()
            )
            for path in sorted(self.folder.iterdir())
            if path.suffix.lower() == ".pdf"
        ]

    def fetch(self, document: SourceDocument) -> bytes:
        return Path(document.locator).read_bytes()


@dataclass(frozen=True)
class ConfluenceSettings:
    base_url: str | None = None
    landing_page: str | None = None
    api_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> ConfluenceSettings:
        return cls(
            base_url=env.get("CFR_CONFLUENCE_BASE_URL"),
            landing_page=env.get("CFR_CONFLUENCE_LANDING_PAGE"),
            api_token=env.get("CFR_CONFLUENCE_API_TOKEN"),
        )

    @property
    def missing(self) -> list[str]:
        values = {
            "CFR_CONFLUENCE_BASE_URL": self.base_url,
            "CFR_CONFLUENCE_LANDING_PAGE": self.landing_page,
            "CFR_CONFLUENCE_API_TOKEN": self.api_token,
        }
        return [name for name, value in values.items() if not value]


class ConfluenceSource:
    """Stub. Still to learn (OQ-30): how the landing page links to documents, the authentication,
    whether each document has a version, and whether the site is reachable."""

    name = "confluence"

    def __init__(self, settings: ConfluenceSettings) -> None:
        self.settings = settings

    def _not_ready(self) -> ProcedureSourceError:
        missing = self.settings.missing
        if missing:
            return ProcedureSourceError(f"Confluence is not configured: set {', '.join(missing)}")
        return ProcedureSourceError("the Confluence source is not built yet (OQ-30): use a PDF folder")

    def list_documents(self) -> list[SourceDocument]:
        raise self._not_ready()

    def fetch(self, document: SourceDocument) -> bytes:
        raise self._not_ready()
