"""A procedure snapshot against real storage: originals restricted, artifacts registered, reused by ref."""

from __future__ import annotations

import pytest

from cfr_v2.artifacts import register
from cfr_v2.contracts import AuthorizationContext, Classification, ProcedureDocument
from cfr_v2.intake.fakes import RegexMasker
from cfr_v2.procedures import ManualPdfSource, build_snapshot, load_snapshot
from cfr_v2.state import CaseStateRepository
from tests.unit.procedures.procedure_pdfs import _page, _pdf

pytestmark = pytest.mark.db

NAME = "Avery Lindqvist"


def test_snapshot_is_stored_registered_and_loaded_by_ref(db, store, tmp_path):
    (tmp_path / "Example Procedure Sep'26.pdf").write_bytes(
        _pdf([_page(["Procedure Name: Example Procedure", "Monthly Revision Date: 1 Sep 2026"]), _page([f"Ask {NAME}"])])
    )
    source = ManualPdfSource(tmp_path)
    result = build_snapshot(source, store=store, masker=RegexMasker([NAME]))
    with db.transaction() as tx:
        repo = CaseStateRepository(tx)
        for stored in result.stored:
            register(repo, stored)
    with db.transaction() as tx:
        rows = tx.all("SELECT classification, count(*) FROM artifact GROUP BY classification ORDER BY 1")
    assert [tuple(r) for r in rows] == [("INTERNAL", 2), ("RESTRICTED", 1)]

    loaded = load_snapshot(store, result.ref)
    assert loaded == result.snapshot
    reader = AuthorizationContext(principal="test", purpose="check")
    raw = store.get(loaded.entries[0].document_ref, classification=Classification.INTERNAL, authorization=reader)
    assert NAME not in ProcedureDocument.model_validate_json(raw).sections[0].text

    again = build_snapshot(source, store=store, masker=RegexMasker([NAME]), previous=loaded)
    assert (again.reused, again.ref) == (1, result.ref)
