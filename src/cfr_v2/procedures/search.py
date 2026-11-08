"""``procedure.search`` (LDR-0019): the capability, and the passages it searches.

``ProcedureSearch`` is the capability; LightRAG, in ``mix`` mode, is the engine behind it
(``lightrag_engine.LightRagProcedureSearch``). A passage is one section of a CURRENT procedure in a
snapshot: superseded versions are never searched.

Each passage carries its procedure name and section title, because some procedures share their
section titles (the three APR procedures, for example).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from cfr_v2.contracts import (
    AuthorizationContext,
    Classification,
    ProcedureDocument,
    ProcedureSearchRequest,
    ProcedureSearchResult,
    ProcedureSection,
    ProcedureSnapshot,
    ProcedureStatus,
)

_READER = AuthorizationContext(principal="procedure.search", purpose="load passages")


@dataclass(frozen=True)
class Passage:
    document: ProcedureDocument
    section: ProcedureSection

    @property
    def text(self) -> str:
        return f"Procedure: {self.document.name}\nSection: {self.section.title}\n\n{self.section.text}"


class ProcedureSearch(Protocol):
    async def search(self, request: ProcedureSearchRequest) -> ProcedureSearchResult: ...


def load_passages(store, refs: Sequence[str]) -> list[Passage]:
    passages = []
    for ref in refs:
        raw = store.get(ref, classification=Classification.INTERNAL, authorization=_READER)
        document = ProcedureDocument.model_validate_json(raw)
        passages += [Passage(document, section) for section in document.sections]
    return passages


def corpus_passages(store, ref: str) -> list[Passage]:
    """The passages of a snapshot's CURRENT procedures.

    ``ref`` may also be an index made by the ``index`` command this replaced: its document list is
    the same snapshot's CURRENT procedures, so a store built from one keeps working.
    """
    raw = store.get(ref, classification=Classification.INTERNAL, authorization=_READER)
    found = json.loads(raw)
    if "entries" in found:
        snapshot = ProcedureSnapshot.model_validate(found)
        return load_passages(store, [e.document_ref for e in snapshot.entries if e.status == ProcedureStatus.CURRENT])
    if "document_refs" in found:
        return load_passages(store, found["document_refs"])
    raise ValueError(f"{ref} is not a procedure snapshot")
