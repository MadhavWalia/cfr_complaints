"""Pass 2's evidence taxonomy comes from the run's pinned release manifest (P4, LDR-0017)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cfr_v2.registry import DEFAULT_REGISTRY_DIR, ManifestError, ReleaseRegistry, pin_manifest
from cfr_v2.registry.evidence_sources import DEFAULT_VERSION, PinnedTaxonomies

pytestmark = pytest.mark.db

REGISTRY = Path(__file__).resolve().parents[2] / DEFAULT_REGISTRY_DIR


def test_taxonomy_follows_the_pinned_manifest(running, store):
    registry = ReleaseRegistry(REGISTRY)
    with_taxonomy = pin_manifest(registry.load("cfr-v2-phase-c"), store=store, db=running.db)
    without = pin_manifest(registry.load("cfr-v2-phase-b"), store=store, db=running.db)
    opened = []

    def db():
        opened.append(1)
        return running.db

    taxonomies = PinnedTaxonomies(store, db, REGISTRY)
    assert taxonomies(with_taxonomy).version == "evidence-sources-1"
    assert taxonomies(without).version == DEFAULT_VERSION  # releases pinned before the field existed
    assert taxonomies(None).version == DEFAULT_VERSION
    taxonomies(with_taxonomy)
    assert len(opened) == 2  # one lookup per manifest; later runs use the cache
    with pytest.raises(ManifestError, match="never pinned"):
        taxonomies("cfr-v2-unknown@000000000000")
