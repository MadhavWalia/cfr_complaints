"""Release manifests: load from the git-tracked registry, pin at admission, resolve later.

A manifest is pinned by freezing its canonical JSON as an INTERNAL artifact and
recording it in ``release_manifest``. Later stages resolve versions through the
pinned snapshot, so editing the YAML never changes a run already admitted (ADR-14).
"""
from __future__ import annotations

from pathlib import Path

import yaml
from sqlalchemy.orm import Session, sessionmaker

from cfr_v2.artifacts import ArtifactStore, register
from cfr_v2.contracts import (
    AuthorizationContext,
    Classification,
    ReleaseManifest,
    canonical_json,
)
from cfr_v2.state import CaseStateRepository

DEFAULT_REGISTRY_DIR = Path("registry")


class ManifestError(ValueError):
    pass


def load_manifest(path: Path) -> ReleaseManifest:
    """Parse a manifest YAML; a declared ``checksum`` must match the content."""
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ManifestError(f"{path}: manifest must be a mapping")
    declared = body.pop("checksum", None)
    manifest = ReleaseManifest.build(**body)
    if declared is not None and declared != manifest.checksum:
        raise ManifestError(f"{path}: declared checksum does not match content")
    check_compatibility(manifest)
    return manifest


def check_compatibility(manifest: ReleaseManifest) -> None:
    programs = [p.program_id for p in manifest.programs]
    if len(programs) != len(set(programs)):
        raise ManifestError(f"{manifest.release_id}: duplicate program ids")
    missing = sorted({s.program_id for s in manifest.skills if s.program_id and s.program_id not in programs})
    if missing:
        raise ManifestError(f"{manifest.release_id}: skills reference unknown programs {missing}")


class ReleaseRegistry:
    """Manifests stored as ``<dir>/releases/<release_id>.yaml``."""

    def __init__(self, directory: Path = DEFAULT_REGISTRY_DIR) -> None:
        self.directory = directory

    def load(self, release_id: str) -> ReleaseManifest:
        path = self.directory / "releases" / f"{release_id}.yaml"
        if not path.is_file():
            raise ManifestError(f"no manifest for release {release_id!r} at {path}")
        manifest = load_manifest(path)
        if manifest.release_id != release_id:
            raise ManifestError(f"{path}: release_id {manifest.release_id!r} does not match file name")
        return manifest


def pin_manifest(manifest: ReleaseManifest, *, store: ArtifactStore, sessions: sessionmaker[Session]) -> str:
    """Freeze the manifest and return the ref recorded on each ``case_run``. Idempotent."""
    stored = store.put(canonical_json(manifest), media_type="application/json", classification=Classification.INTERNAL)
    with sessions.begin() as session:
        repo = CaseStateRepository(session)
        register(repo, stored)
        repo.register_manifest(manifest, artifact_ref=stored.metadata.ref)
    return manifest.ref


def load_pinned(
    manifest_ref: str,
    *,
    store: ArtifactStore,
    sessions: sessionmaker[Session],
    authorization: AuthorizationContext,
) -> ReleaseManifest:
    with sessions() as session:
        artifact_ref = CaseStateRepository(session).manifest_artifact_ref(manifest_ref)
    if artifact_ref is None:
        raise ManifestError(f"manifest {manifest_ref!r} was never pinned")
    data = store.get(artifact_ref, classification=Classification.INTERNAL, authorization=authorization)
    return ReleaseManifest.model_validate_json(data)
