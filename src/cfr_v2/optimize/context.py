"""What gold specs read (LDR-0027): case runs and their committed results, the artifact store,
and the current release's routing inputs. Everything opens on first use.

A downstream prompt's inputs come from **committed** results of the stages before it, frozen
when the template is made, so each prompt is optimized on its own without re-running anything.
The current release (``CFR_RELEASE_ID``) supplies the evidence-source taxonomy and the vertical
definitions, because an optimized prompt ships in the next release built from it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from functools import cached_property
from pathlib import Path
from typing import Any

from cfr_v2.contracts import AuthorizationContext, Classification, IngestionRequest

READER = AuthorizationContext(
    principal="optimize",
    purpose="prompt optimization against a gold set",
    allowed_classifications=frozenset({"INTERNAL", "RESTRICTED"}),  # Pass 1 gold quotes sit in the original
)


class Context:
    def __init__(self, root: Path | None = None, env: Mapping[str, str] | None = None) -> None:
        self.root = root or Path.cwd()
        self.env = os.environ if env is None else env

    @cached_property
    def store(self):
        from cfr_v2.artifacts import S3ArtifactStore, S3Settings

        return S3ArtifactStore(S3Settings.from_env(self.env))

    @cached_property
    def db(self):
        from cfr_v2.runtime.settings import RuntimeSettings
        from cfr_v2.state import Database

        return Database(RuntimeSettings.from_env(self.env).state_database_url, max_connections=4)

    def close(self) -> None:
        if "db" in self.__dict__:
            self.db.close()

    # --- cases ----------------------------------------------------------------------------

    def runs(self, case_nos: Sequence[str]) -> dict[str, tuple[str, str, str]]:
        """case_no -> (case_run_id, request_ref, release_manifest_ref) of its latest run."""
        from cfr_v2.understanding.score import latest_runs

        return latest_runs(self.db, list(case_nos)) if case_nos else {}

    def committed(self, run_id: str, task: str, model) -> Any:
        """The run's committed result for ``task``, parsed as ``model``, with its artifact ref; (None, None) if none."""
        from cfr_v2.state import CaseStateRepository

        with self.db.transaction() as tx:
            done = [c for c in CaseStateRepository(tx).committed_results(run_id) if c.logical_task_id == task]
        if not done:
            return None, None
        ref = done[-1].output_artifact_refs[0]
        return model.model_validate_json(self.get(ref)), ref

    def get(self, ref: str, classification: Classification = Classification.INTERNAL) -> bytes:
        return self.store.get(ref, classification=classification, authorization=READER)

    def request(self, request_ref: str) -> IngestionRequest:
        return IngestionRequest.model_validate_json(self.get(request_ref, Classification.RESTRICTED))

    # --- the current release's routing inputs --------------------------------------------------

    @cached_property
    def release(self):
        from cfr_v2.intake.settings import IntakeSettings
        from cfr_v2.registry import ReleaseRegistry

        return ReleaseRegistry(self.root / "registry").load(IntakeSettings.from_env(self.env).release_id)

    @cached_property
    def taxonomy(self):
        from cfr_v2.registry.evidence_sources import DEFAULT_VERSION, load_taxonomy

        return load_taxonomy(self.release.evidence_sources or DEFAULT_VERSION, self.root / "registry")

    @cached_property
    def catalog(self):
        from cfr_v2.routing.verticals import VerticalCatalog

        if not self.release.routing_verticals:
            raise ValueError(f"release {self.release.release_id} pins no routing_verticals")
        return VerticalCatalog.parse(self.get(self.release.routing_verticals).decode("utf-8"))

    def passages(self, procedures_ref: str) -> dict[tuple[str, int, int], Any]:
        """A snapshot's passages by (source sha256, section index, part); cached per snapshot."""
        cache = self.__dict__.setdefault("_passages", {})
        if procedures_ref not in cache:
            from cfr_v2.procedures import corpus_passages

            cache[procedures_ref] = {
                (p.document.source_sha256, p.section.index, p.section.part): p
                for p in corpus_passages(self.store, procedures_ref)
            }
        return cache[procedures_ref]


__all__ = ["READER", "Context"]
