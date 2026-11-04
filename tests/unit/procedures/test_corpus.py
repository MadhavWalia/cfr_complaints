"""Procedure snapshots (LDR-0019): sources, redaction, version choice, and the numbers-only output."""

from __future__ import annotations

from datetime import date

import pytest

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.contracts import (
    Classification,
    ExtractionQuality,
    ProcedureDocument,
    ProcedureStatus,
    VersionFlag,
)
from cfr_v2.intake.fakes import RegexMasker
from cfr_v2.procedures import (
    ConfluenceSettings,
    ConfluenceSource,
    ManualPdfSource,
    ProcedureSourceError,
    build_snapshot,
    choose_current,
    corpus,
)
from cfr_v2.procedures import __main__ as cli
from tests.unit.procedures.procedure_pdfs import _page, _pdf

NAME = "Avery Lindqvist"
SECRET = "SECRET-BODY-TEXT"
BODY = f"Escalate to {NAME} at avery@example.com when the {SECRET} applies to the request on the account"


def _procedure(name: str, monthly: str, *, extra: str = "") -> bytes:
    front = [
        f"Procedure Name: {name}",
        "Annual Review Effective Date: 1 March 2026",
        f"Monthly Revision Effective Date: {monthly}",
        "Custodian Approval Required: Yes",
    ]
    contents = ["Table of Contents", "1 Purpose ... 3", "2 Scope ... 3", "3 Rules ... 3"]
    body = ["1 Purpose", BODY, "2 Scope", BODY, "3 Rules", BODY, extra]
    return _pdf([_page(front), _page(contents), _page(body)])


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "Example Procedure Aug'26.pdf").write_bytes(_procedure("Example Procedure", "1 August 2026"))
    (tmp_path / "Example Procedure Sep'26.pdf").write_bytes(_procedure("Example Procedure", "1 September 2026"))
    (tmp_path / "Other Procedure.pdf").write_bytes(_procedure("Other Procedure", "1 September 2026"))
    (tmp_path / "Other Procedure (1).pdf").write_bytes(_procedure("Other Procedure", "1 September 2026"))
    (tmp_path / "notes.txt").write_text("ignored")
    return tmp_path


def _build(folder, store=None, **kwargs):
    store = store or InMemoryArtifactStore()
    masker = kwargs.pop("masker", RegexMasker([NAME]))
    return store, build_snapshot(ManualPdfSource(folder), store=store, masker=masker, **kwargs)


def test_identical_files_are_one_entry_and_the_latest_version_is_current(folder):
    _, result = _build(folder)
    status = {e.file_names: e.status for e in result.snapshot.entries}
    assert status == {
        ("Example Procedure Aug'26.pdf",): ProcedureStatus.SUPERSEDED,
        ("Example Procedure Sep'26.pdf",): ProcedureStatus.CURRENT,
        ("Other Procedure (1).pdf", "Other Procedure.pdf"): ProcedureStatus.CURRENT,
    }
    assert not any(e.flags for e in result.snapshot.entries)


def test_a_shared_front_page_name_does_not_replace_another_procedure(tmp_path, monkeypatch, capsys):
    (tmp_path / "Limit Management Sep'26.pdf").write_bytes(_procedure("Shared Heading", "1 September 2026"))
    (tmp_path / "Business Loans.pdf").write_bytes(_procedure("Shared Heading", "1 August 2026"))
    monkeypatch.setattr(cli, "_masker", lambda: RegexMasker([NAME]))
    assert cli.main(["extract", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert "2 distinct files, 2 procedures" in printed
    assert "SUPERSEDE" not in printed
    assert "Business Loans.pdf | Limit Management Sep'26.pdf" in printed


def test_only_originals_are_restricted_and_internal_text_is_redacted(folder):
    store, result = _build(folder)
    internal = b"".join(data for (kind, _), data in store.objects.items() if kind == Classification.INTERNAL)
    assert NAME.encode() not in internal and b"avery@example.com" not in internal
    assert SECRET.encode() in internal  # the text itself is kept
    originals = [data for (kind, _), data in store.objects.items() if kind == Classification.RESTRICTED]
    assert len(originals) == 3 and all(d.startswith(b"%PDF") for d in originals)
    document = next(iter(result.documents.values()))
    assert all(s.pii_masked == 2 for s in document.sections)


def test_a_title_is_redacted_too(tmp_path):
    (tmp_path / "x.pdf").write_bytes(
        _pdf(
            [
                _page(["Procedure Name: X"]),
                _page(["Table of Contents", f"1 Ask {NAME} ... 3", "2 Scope ... 3", "3 Rules ... 3"]),
                _page([f"1 Ask {NAME}", BODY, "2 Scope", BODY, "3 Rules", BODY]),
            ]
        )
    )
    _, result = _build(tmp_path)
    [document] = result.documents.values()
    assert NAME not in document.sections[0].title and "⟦PII⟧" in document.sections[0].title


def test_a_document_that_cannot_be_read_or_redacted_is_reported_and_not_stored(tmp_path):
    class Broken:
        version = "broken"

        def mask(self, text):
            raise ValueError(text)

    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")
    (tmp_path / "good.pdf").write_bytes(_procedure("Good", "1 March 2026"))
    store, result = _build(tmp_path, masker=Broken())
    assert {f.file_name for f in result.snapshot.failures} == {"bad.pdf", "good.pdf"}
    assert all("Good" not in f.error and SECRET not in f.error for f in result.snapshot.failures)
    assert result.snapshot.entries == ()
    assert not [k for k in store.objects if k[0] == Classification.RESTRICTED]


def test_unchanged_pdfs_are_not_read_again(folder, monkeypatch):
    store, first = _build(folder)
    new = folder / "New Procedure.pdf"
    new.write_bytes(_procedure("New Procedure", "1 October 2026"))
    calls = []
    real = corpus.extract_pdf
    monkeypatch.setattr(corpus, "extract_pdf", lambda data: calls.append(1) or real(data))
    _, second = _build(folder, store, previous=first.snapshot)
    assert (len(calls), second.reused) == (1, 3)
    assert {e.document_ref for e in first.snapshot.entries} < {e.document_ref for e in second.snapshot.entries}
    same = [e for e in second.snapshot.entries if e.source_sha256 in first.documents]
    assert {e.source_ref for e in same} == {e.source_ref for e in first.snapshot.entries}


def test_a_new_masker_reads_everything_again(folder, monkeypatch):
    store, first = _build(folder)

    class Newer(RegexMasker):
        version = "regex-fake-2"

    _, second = _build(folder, store, previous=first.snapshot, masker=Newer([NAME]))
    assert second.reused == 0


def test_the_same_input_gives_the_same_snapshot(folder):
    (_, first), (_, second) = _build(folder), _build(folder)
    assert first.ref == second.ref


def _version(
    file_name: str, monthly: date | None = None, annual: date | None = None, file_date: date | None = None
) -> ProcedureDocument:
    return ProcedureDocument(
        file_name=file_name,
        source_sha256="0" * 64,
        name="X",
        procedure_key="x",
        annual_review_date=annual,
        monthly_revision_date=monthly,
        file_date=file_date,
        extractor_version="e",
        redaction_version="r",
        quality=ExtractionQuality(pages=1, method="font"),
        sections=(),
    )


JAN, FEB, MAR = date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)


def test_monthly_date_beats_annual_and_file_dates():
    best, flags = choose_current([_version("a", JAN, MAR, MAR), _version("b", FEB, JAN, JAN)])
    assert (best.file_name, flags) == ("b", ())


def test_annual_date_decides_when_monthly_dates_match_or_are_missing():
    assert choose_current([_version("a", JAN, JAN), _version("b", JAN, FEB)])[0].file_name == "b"
    best, flags = choose_current([_version("a", None, JAN, MAR), _version("b", None, FEB, JAN)])
    assert (best.file_name, flags) == ("b", ())


def test_a_decision_by_file_date_is_flagged():
    best, flags = choose_current([_version("a", JAN, JAN, JAN), _version("b", JAN, JAN, FEB)])
    assert (best.file_name, flags) == ("b", (VersionFlag.FILE_DATE_USED,))


def test_a_single_document_without_front_dates_is_flagged():
    assert choose_current([_version("a", file_date=JAN)])[1] == (VersionFlag.FILE_DATE_USED,)
    assert choose_current([_version("a", annual=JAN)])[1] == ()


def test_a_tie_is_flagged():
    assert choose_current([_version("a", JAN, JAN, JAN), _version("b", JAN, JAN, JAN)])[1] == (VersionFlag.TIED,)


def test_a_missing_folder_is_a_clear_error(tmp_path):
    with pytest.raises(ProcedureSourceError, match="not a folder"):
        ManualPdfSource(tmp_path / "nowhere").list_documents()


def test_confluence_says_what_is_missing_and_hides_the_token():
    settings = ConfluenceSettings.from_env(
        {"CFR_CONFLUENCE_API_TOKEN": "tok-secret", "CFR_CONFLUENCE_LANDING_PAGE": "1"}
    )
    with pytest.raises(ProcedureSourceError, match="CFR_CONFLUENCE_BASE_URL"):
        ConfluenceSource(settings).list_documents()
    assert "tok-secret" not in repr(settings)


def test_configured_confluence_is_still_a_stub():
    settings = ConfluenceSettings("https://example.test", "1", "t")
    with pytest.raises(ProcedureSourceError, match="not built yet"):
        ConfluenceSource(settings).list_documents()


def test_extract_prints_no_document_text_and_writes_redacted_text_locally(folder, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_masker", lambda: RegexMasker([NAME]))
    out = tmp_path / "trial"
    assert cli.main(["extract", str(folder), "--out", str(out)]) == 0
    printed = capsys.readouterr()
    for text in (SECRET, NAME, "Purpose"):
        assert text not in printed.out + printed.err
    assert "Example Procedure Sep'26.pdf > Example Procedure Aug'26.pdf" in printed.out
    assert "3 distinct files, 2 procedures" in printed.out
    review = (out / "Example Procedure Sep'26.md").read_text(encoding="utf-8")
    assert "## 1 Purpose" in review and "_page 3_" in review and SECRET in review and NAME not in review


def test_a_failed_document_gives_a_failing_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_masker", lambda: RegexMasker())
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")
    assert cli.main(["extract", str(tmp_path)]) == 1
    assert "could not prepare" in capsys.readouterr().out


def test_confluence_from_the_command_line_explains_itself(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_masker", lambda: RegexMasker())
    assert cli.main(["extract", "--confluence"]) == 1
    assert "not configured" in capsys.readouterr().err


def test_a_folder_or_confluence_is_required():
    with pytest.raises(SystemExit):
        cli.main(["extract"])
