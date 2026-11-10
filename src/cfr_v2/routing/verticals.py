"""The vertical definitions the router routes to (C04, LDR-0019).

They come from the business's knowledge document: per vertical, routing keywords, an op-risk
principle, archetypal failures, exclusions and boundary rules. The document is internal and
stays out of this repository: it is read from a path, and its hash is recorded with every result.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

UNMAPPED_NAME = "UNMAPPED OPERATIONAL RISK"  # a taxonomy-gap marker, not a vertical (HD-18)
_INDEX = "## VERTICAL INDEX"
_ENTRY = re.compile(r"^\*\*([^*:(]+?)\s*(?:\(([^)]*)\))?\s*:\*\*", re.MULTILINE)


class VerticalDocumentError(ValueError):
    pass


@dataclass(frozen=True)
class Vertical:
    vertical_id: str  # as the document names it: "RLA", "NEW ACCOUNTS"
    full_name: str | None = None  # "RESPONSIBLE LENDING ACTIONS"


@dataclass(frozen=True)
class VerticalCatalog:
    text: str
    verticals: tuple[Vertical, ...]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(v.vertical_id for v in self.verticals)

    @classmethod
    def parse(cls, text: str) -> VerticalCatalog:
        """The verticals are the entries of the document's keyword index, less UNMAPPED."""
        if _INDEX not in text:
            raise VerticalDocumentError(f"no '{_INDEX}' section: is this the knowledge document?")
        index = text.split(_INDEX, 1)[1].split("\n## ", 1)[0]
        entries = [(m.group(1).strip(), m.group(2)) for m in _ENTRY.finditer(index)]
        verticals = tuple(Vertical(name, full and full.strip()) for name, full in entries if name != UNMAPPED_NAME)
        if not verticals:
            raise VerticalDocumentError("the vertical index lists no verticals")
        if len({v.vertical_id for v in verticals}) != len(verticals):
            raise VerticalDocumentError("the vertical index names a vertical twice")
        return cls(text, verticals)

    @classmethod
    def load(cls, path: Path) -> VerticalCatalog:
        return cls.parse(path.read_text(encoding="utf-8"))
