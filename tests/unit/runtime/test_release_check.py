"""Release compatibility checks (LDR-0022): the default release matches the code; intake and a worker
refuse releases that don't."""

from __future__ import annotations

from pathlib import Path

import pytest

from cfr_v2.intake.settings import IntakeSettings
from cfr_v2.registry import DEFAULT_REGISTRY_DIR, ManifestError, ReleaseRegistry
from cfr_v2.runtime.release_check import (
    IncompatibleReleaseError,
    assess_queue,
    over_cap,
    queue_problems,
    require_compatible,
    stage_mismatches,
)

REGISTRY = ReleaseRegistry(Path(__file__).resolve().parents[3] / DEFAULT_REGISTRY_DIR)


def test_the_default_release_matches_the_stages_this_code_runs():
    """Change a stage's version, and this fails until a release pins the new one and becomes the
    default: otherwise every run admitted would be refused by the worker."""
    manifest = REGISTRY.load(IntakeSettings().release_id)
    assert stage_mismatches(manifest) == [], f"add a release pinning {[str(m) for m in stage_mismatches(manifest)]}"
    require_compatible(manifest)


def test_an_older_release_names_what_differs():
    manifest = REGISTRY.load("cfr-v2-phase-d")
    [mismatch] = stage_mismatches(manifest)
    assert (mismatch.stage, mismatch.code_version, mismatch.release_version) == (
        "c04.routing",
        "route-1",
        "placeholder-0",
    )
    with pytest.raises(IncompatibleReleaseError, match="c04.routing: code runs route-1, release pins placeholder-0"):
        require_compatible(manifest)
    missing = {m.stage: m.release_version for m in stage_mismatches(REGISTRY.load("cfr-v2-phase-b"))}
    assert missing["c02.pass1"] is None  # phase B named one c02.understanding program


def _resolve(ref):
    if ref.startswith("cfr-v2-phase-"):
        return REGISTRY.load(ref.split("@")[0])
    raise ManifestError(f"manifest {ref!r} was never pinned")


def test_a_worker_sees_the_queue_by_release_and_what_it_cannot_run():
    rows = [
        ("cfr-v2-phase-e@1", "QUEUED", 3),
        ("cfr-v2-phase-d@2", "QUEUED", 8000),
        ("cfr-v2-phase-d@2", "RUNNING", 17),
        ("spike@0", "QUEUED", 1),
    ]
    releases = {r.manifest_ref: r for r in assess_queue(rows, _resolve)}
    assert releases["cfr-v2-phase-e@1"].compatible and releases["cfr-v2-phase-e@1"].queued == 3
    old = releases["cfr-v2-phase-d@2"]
    assert (old.queued, old.running, old.compatible) == (8000, 17, False)
    assert releases["spike@0"].unknown
    problems = queue_problems(list(releases.values()))
    assert len(problems) == 2
    assert any("8000 queued and 17 running under cfr-v2-phase-d@2" in p and "c04.routing" in p for p in problems)
    assert any("never pinned" in p for p in problems)


def test_nothing_queued_or_only_current_work_is_fine_up_to_the_cap():
    assert queue_problems(assess_queue([], _resolve)) == []
    current = assess_queue([("cfr-v2-phase-e@1", "QUEUED", 12)], _resolve)
    assert queue_problems(current) == [] and over_cap(current, None) is None
    assert over_cap(current, 12) is None
    assert over_cap(current, 10) == "12 jobs queued, more than --max-queued 10"


def test_intake_refuses_a_release_that_does_not_match_the_code(monkeypatch, capsys):
    from cfr_v2.intake import __main__ as intake

    monkeypatch.setenv("CFR_RELEASE_ID", "cfr-v2-phase-d")
    monkeypatch.setenv("CFR_REGISTRY_DIR", str(REGISTRY.directory))
    with pytest.raises(SystemExit) as exc:
        intake.main(["--synthetic", "1"])
    assert exc.value.code == 2
    assert "c04.routing: code runs route-1, release pins placeholder-0" in capsys.readouterr().err
