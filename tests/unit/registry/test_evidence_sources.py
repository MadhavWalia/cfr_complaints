"""Evidence source taxonomy: registry YAML, lint, and the business workbook (backends, proposals, changes)."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest
import yaml
from openpyxl import load_workbook

from cfr_v2.contracts import BackendStatus, EvidenceSourceTaxonomy
from cfr_v2.registry.evidence_sources import (
    DEFAULT_VERSION,
    EvidenceSourceError,
    import_workbook,
    lint,
    load_taxonomy,
    main,
    requests_path_for,
    save,
    to_workbook,
)

REPO_REGISTRY = Path(__file__).resolve().parents[3] / "registry"


@pytest.fixture
def registry(tmp_path):
    shutil.copytree(REPO_REGISTRY / "evidence_sources", tmp_path / "registry" / "evidence_sources")
    return tmp_path / "registry"


@pytest.fixture
def book(registry, tmp_path):
    path = tmp_path / "sources.xlsx"
    to_workbook(load_taxonomy(DEFAULT_VERSION, registry), path, registry)
    return path


def _set(book: Path, sheet: str, row: int, column: str, value) -> None:
    wb = load_workbook(book)
    ws = wb[sheet]
    header = [c.value for c in ws[1]]
    ws.cell(row=row, column=header.index(column) + 1, value=value)
    wb.save(book)


def _append(book: Path, sheet: str, values: list) -> None:
    wb = load_workbook(book)
    wb[sheet].append(values)
    wb.save(book)


def test_every_version_in_the_repo_loads_and_passes_lint():
    assert main(["--registry", str(REPO_REGISTRY), "check"]) == 0
    taxonomy = load_taxonomy(DEFAULT_VERSION, REPO_REGISTRY)
    assert lint(taxonomy) == []
    assert {"transactions", "procedure", "other"} <= {t.id for t in taxonomy.active()}


def test_unchanged_workbook_imports_as_a_no_op(registry, book):
    before = load_taxonomy(DEFAULT_VERSION, registry)
    report = import_workbook(book, registry)
    assert (report.added, report.removed, report.changed) == ([], [], [])
    assert load_taxonomy(DEFAULT_VERSION, registry) == before


def test_backend_edits_are_applied_and_reported(registry, book):
    _set(book, "backends", 2, "owner", "Payments data team")  # first row: transactions
    _append(book, "backends", ["fees_and_interest", "LUMI (BigQuery)", "axp-lumi.dw.example_fees", "planned", "", ""])
    report = import_workbook(book, registry)
    assert report.added == ["fees_and_interest: LUMI (BigQuery) axp-lumi.dw.example_fees [planned]"]
    assert len(report.changed) == 1 and report.removed == []
    taxonomy = load_taxonomy(DEFAULT_VERSION, registry)
    fees = next(t for t in taxonomy.source_types if t.id == "fees_and_interest")
    assert {b.status for b in fees.backends} == {BackendStatus.NONE, BackendStatus.PLANNED}
    assert taxonomy.source_types[0].backends[0].owner == "Payments data team"


def test_edit_to_the_locked_source_types_sheet_is_refused(registry, book):
    wb = load_workbook(book)
    wb["source_types"].protection.sheet = False  # someone unlocks it
    wb["source_types"]["B2"] = "Changed wording"
    wb.save(book)
    with pytest.raises(EvidenceSourceError, match="proposals sheet"):
        import_workbook(book, registry)


@pytest.mark.parametrize(
    ("row", "error"),
    [
        (["transacions", "LUMI", "", "available", "", ""], "unknown source types"),  # typo'd id
        (["transactions", "LUMI", "", "maybe", "", ""], "status"),
        (["transactions", "", "", "available", "", ""], "system is empty"),
        (["transactions", "LUMI (BigQuery)", "axp-lumi.dw.wwcas_authorization", "planned", "", ""], "duplicate"),
    ],
)
def test_bad_backend_rows_are_refused(registry, book, row, error):
    _append(book, "backends", row)
    with pytest.raises(EvidenceSourceError, match=error):
        import_workbook(book, registry)


def test_every_source_type_keeps_a_backend_row(registry, book):
    wb = load_workbook(book)
    ws = wb["backends"]
    for row in list(ws.iter_rows(min_row=2)):
        if row[0].value == "disputes":
            ws.delete_rows(row[0].row)
    wb.save(book)
    with pytest.raises(EvidenceSourceError, match="needs a backends row"):
        import_workbook(book, registry)


def test_proposals_and_changes_are_logged_once_and_shown_on_export(registry, book, tmp_path):
    _append(
        book,
        "proposals",
        ["new source type", "", "Investigators check credit bureau reports", "", "AB", dt.date(2026, 11, 3), ""],
    )
    _append(book, "changes", [dt.date(2026, 11, 3), "AB", "Added fees owner", "Owner confirmed"])
    report = import_workbook(book, registry)
    assert (report.new_proposals, report.new_changes) == (1, 1)
    assert import_workbook(book, registry).new_proposals == 0  # importing again adds nothing

    log = yaml.safe_load(requests_path_for(DEFAULT_VERSION, registry).read_text())
    assert log["proposals"][0]["status"] == "open" and log["proposals"][0]["date"] == "2026-11-03"
    again = tmp_path / "again.xlsx"
    to_workbook(load_taxonomy(DEFAULT_VERSION, registry), again, registry)
    rows = list(load_workbook(again)["proposals"].iter_rows(min_row=2, values_only=True))
    assert rows[0][2] == "Investigators check credit bureau reports" and rows[0][6] == "open"


def test_proposal_needs_a_kind_and_text(registry, book):
    _append(book, "proposals", ["rename", "", "x", "", "AB", "", ""])
    with pytest.raises(EvidenceSourceError, match="kind"):
        import_workbook(book, registry)


def test_workbook_guards_against_entry_errors(book):
    wb = load_workbook(book)
    assert wb["source_types"].protection.sheet
    formulas = {v.formula1 for v in wb["backends"].data_validations.dataValidation}
    assert any(f.startswith("=source_types!") for f in formulas)  # source_type is a dropdown of real ids
    assert any("available" in f for f in formulas)
    assert wb["backends"].conditional_formatting  # duplicate rows highlighted


def test_lint_catches_unclear_text():
    backends = [{"system": "x", "status": "none"}]
    taxonomy = EvidenceSourceTaxonomy.model_validate(
        {
            "version": "v",
            "source_types": [
                {"id": "a", "description": "short", "examples": ["was it charged?"], "backends": backends},
                {
                    "id": "b",
                    "description": "A long enough description here.",
                    "examples": ["Was it charged?", "no question mark"],
                    "backends": backends,
                },
                {"id": "c", "description": "A long enough description here.", "backends": backends},
            ],
        }
    )
    problems = "\n".join(lint(taxonomy))
    assert "a: description" in problems
    assert "b: example also used by a" in problems
    assert "b: example should be a question" in problems
    assert "c: give 1-3 examples" in problems


def test_new_model_facing_text_needs_a_new_version(registry):
    taxonomy = load_taxonomy(DEFAULT_VERSION, registry)
    first = taxonomy.source_types[0].model_copy(update={"description": "Changed wording for the model."})
    with pytest.raises(EvidenceSourceError, match="new version"):
        save(taxonomy.model_copy(update={"source_types": (first, *taxonomy.source_types[1:])}), registry)


def test_deprecation_needs_an_active_replacement():
    body = {
        "version": "v",
        "source_types": [
            {
                "id": "old",
                "description": "d",
                "deprecated": True,
                "replaced_by": "new",
                "backends": [{"system": "x", "status": "none"}],
            },
        ],
    }
    with pytest.raises(ValueError):
        EvidenceSourceTaxonomy.model_validate(body)
    body["source_types"].append({"id": "new", "description": "d", "backends": [{"system": "x", "status": "planned"}]})
    assert [t.id for t in EvidenceSourceTaxonomy.model_validate(body).active()] == ["new"]
