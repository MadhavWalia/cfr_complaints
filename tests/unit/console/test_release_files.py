"""A new release file written from the console (LDR-0025 step 3)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cfr_v2.console.release_files import ReleaseFileError, write_release
from cfr_v2.registry import ReleaseRegistry

REPO = Path(__file__).resolve().parents[3]
SNAPSHOT = "sha256:b72453c4ef4cae8c944af6bacd14fcdfefab011b904de80f22669a9fae47aa8b"


@pytest.fixture
def registry(tmp_path):
    shutil.copytree(REPO / "registry", tmp_path / "registry")
    return tmp_path / "registry"


def test_a_new_release_copies_another_and_changes_only_its_routing_pins(registry):
    base = ReleaseRegistry(registry).load("cfr-v2-phase-e")
    path = write_release(registry, "cfr-v2-phase-e", "cfr-v2-phase-f", {"procedure_snapshot": SNAPSHOT})
    new = ReleaseRegistry(registry).load("cfr-v2-phase-f")
    assert path == registry / "releases" / "cfr-v2-phase-f.yaml"
    assert new.release_id == "cfr-v2-phase-f" and new.procedure_snapshot == SNAPSHOT
    assert new.programs == base.programs and new.routing_verticals == base.routing_verticals
    source = (registry / "releases" / "cfr-v2-phase-e.yaml").read_text()
    comments = [line for line in source.splitlines() if line.startswith("#")]
    assert comments and all(line in path.read_text() for line in comments)  # comments kept


@pytest.mark.parametrize(
    ("base", "new", "pins", "message"),
    [
        ("cfr-v2-phase-e", "cfr-v2-phase-d", {}, "already exists"),
        ("cfr-v2-phase-e", "Phase F", {}, "lower-case letters"),
        ("cfr-v2-phase-e", "../x", {}, "lower-case letters"),
        ("nope", "cfr-v2-phase-f", {}, "no release nope"),
        ("cfr-v2-phase-e", "cfr-v2-phase-f", {"procedure_snapshot": "sha256:abc"}, "a full ref"),
    ],
)
def test_a_new_release_is_refused_rather_than_written_wrong(registry, base, new, pins, message):
    before = sorted(p.name for p in (registry / "releases").iterdir())
    with pytest.raises(ReleaseFileError, match=message):
        write_release(registry, base, new, pins)
    assert sorted(p.name for p in (registry / "releases").iterdir()) == before  # nothing written


def test_a_file_that_would_not_load_is_removed(registry):
    (registry / "releases" / "broken.yaml").write_text("release_id: broken\nprograms: not-a-list\n")
    with pytest.raises(ReleaseFileError, match="wouldn't load"):
        write_release(registry, "broken", "broken-2", {})
    assert not (registry / "releases" / "broken-2.yaml").exists()
