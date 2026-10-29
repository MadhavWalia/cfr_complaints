from __future__ import annotations

from pathlib import Path

import pytest

from cfr_v2.contracts import ProgramRef, ReleaseManifest
from cfr_v2.registry import (
    DEFAULT_REGISTRY_DIR,
    ManifestError,
    ReleaseRegistry,
    load_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BODY = """release_id: r1
programs:
  - {program_id: p1, version: "1", module: m:f}
skills: []
capabilities: []
"""


def _write(tmp_path: Path, text: str, name: str = "r1") -> Path:
    path = tmp_path / "releases" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_packaged_release_loads():
    manifest = ReleaseRegistry(REPO_ROOT / DEFAULT_REGISTRY_DIR).load("cfr-v2-phase-b")
    assert {p.program_id for p in manifest.programs} == {
        "c02.understanding",
        "c03.primary_selection",
        "c04.routing",
        "c05.planning",
    }


def test_build_from_plain_dicts_matches_build_from_models():
    a = ReleaseManifest.build(release_id="r", programs=[{"program_id": "p", "version": "1", "module": "m"}])
    b = ReleaseManifest.build(release_id="r", programs=(ProgramRef(program_id="p", version="1", module="m"),))
    assert a == b and a.checksum == b.checksum


def test_same_content_same_ref(tmp_path):
    first = load_manifest(_write(tmp_path, BODY))
    second = load_manifest(_write(tmp_path, BODY.replace("programs:", "# comment\nprograms:")))
    assert first.ref == second.ref


def test_declared_checksum_must_match(tmp_path):
    with pytest.raises(ManifestError, match="checksum"):
        load_manifest(_write(tmp_path, BODY + "checksum: " + "0" * 64 + "\n"))
    good = load_manifest(_write(tmp_path, BODY))
    assert load_manifest(_write(tmp_path, BODY + f"checksum: {good.checksum}\n")) == good


def test_skill_referencing_unknown_program_rejected(tmp_path):
    text = BODY.replace("skills: []", "skills:\n  - {vertical_id: V, skill_version: '1', program_id: missing}")
    with pytest.raises(ManifestError, match="unknown programs"):
        load_manifest(_write(tmp_path, text))


def test_release_id_must_match_file_name(tmp_path):
    _write(tmp_path, BODY, name="other")
    with pytest.raises(ManifestError, match="does not match"):
        ReleaseRegistry(tmp_path).load("other")


def test_unknown_release(tmp_path):
    with pytest.raises(ManifestError, match="no manifest"):
        ReleaseRegistry(tmp_path).load("nope")
