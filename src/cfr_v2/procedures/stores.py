"""Where a snapshot's LightRAG store lives (LDR-0021): one folder per snapshot, named after it.

A store holds the passages of one snapshot only (LightRAG never removes a passage, so two would
mix), and records that snapshot in ``cfr_store.json``. Naming the folder after the snapshot lets
everything that searches find the store from the ref alone: the worker from the release's
``procedure_snapshot``, the CLIs from the ref they are given. ``CFR_LIGHTRAG_WORKDIR`` overrides
the folder, for a store kept somewhere else.

A store's marker also says whether it is ``complete`` or part-way through an append
(``appending``, LDR-0043), and how it came to be (``lineage``): an append moves a store in place from
one snapshot to a later one, then renames its folder after the new snapshot.

Light on purpose: no LightRAG import, so the console can use it.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WORKDIR_ENV = "CFR_LIGHTRAG_WORKDIR"
STORE_MARKER = "cfr_store.json"  # in the store: the snapshot it was built from
STORE_PREFIX = "lightrag_store_"
COMPLETE, APPENDING = "complete", "appending"
# Files LightRAG writes in a store with its default (JSON) storages: one of them makes a folder a store
LIGHTRAG_FILES = ("kv_store_doc_status.json", "graph_chunk_entity_relation.graphml", "vdb_chunks.json")


def store_name(procedures_ref: str) -> str:
    """``lightrag_store_`` and the first 12 characters of the ref's hash."""
    digest = procedures_ref.removeprefix("sha256:")
    if not re.fullmatch(r"[A-Za-z0-9]+", digest):
        raise ValueError(f"not a snapshot ref: {procedures_ref!r}")
    return STORE_PREFIX + digest[:12]


def store_dir(procedures_ref: str, *, root: Path = Path(), env: Mapping[str, str] = os.environ) -> Path:
    """The store searched for ``procedures_ref``: ``CFR_LIGHTRAG_WORKDIR`` when set, else the
    snapshot's own folder, under ``root``."""
    return root / (env.get(WORKDIR_ENV) or store_name(procedures_ref))


def read_marker(workdir: Path) -> dict[str, Any] | None:
    """The store's marker, or None when it has none (or it can't be read)."""
    try:
        marker = json.loads((workdir / STORE_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def write_marker(workdir: Path, marker: Mapping[str, Any]) -> None:
    """Written whole or not at all: a crash mid-write leaves the previous marker."""
    path = workdir / STORE_MARKER
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(dict(marker), indent=2) + "\n", encoding="utf-8")
    os.replace(part, path)


def state(marker: Mapping[str, Any] | None) -> str | None:
    """``complete`` or ``appending``; a marker from before appends has no state and is complete."""
    return None if marker is None else str(marker.get("state") or COMPLETE)


def built_from(workdir: Path) -> str | None:
    """The snapshot a complete store holds; None when it records none, is missing, or is part-way
    through an append (it then holds neither snapshot whole)."""
    marker = read_marker(workdir)
    if marker is None or state(marker) != COMPLETE:
        return None
    return marker.get("procedures_ref")


def is_store(folder: Path) -> bool:
    """A folder with a marker or LightRAG's files: what Remove a store may delete, and nothing else."""
    return folder.is_dir() and (
        (folder / STORE_MARKER).is_file() or any((folder / name).is_file() for name in LIGHTRAG_FILES)
    )


def size(folder: Path) -> int:
    """Bytes in the folder, all the way down."""
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def pinned_snapshots(registry: Path) -> dict[str, list[str]]:
    """Snapshot ref -> the releases in ``registry/releases`` that pin it."""
    import yaml

    found: dict[str, list[str]] = {}
    for path in sorted((registry / "releases").glob("*.yaml")):
        try:
            pinned = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("procedure_snapshot")
        except (OSError, yaml.YAMLError, AttributeError):
            continue
        if isinstance(pinned, str) and pinned:
            found.setdefault(pinned, []).append(path.stem)
    return found


@dataclass(frozen=True)
class StoreInfo:
    folder: Path
    procedures_ref: str | None  # what it holds (for an append in progress: what it held before)
    state: str | None  # complete, appending, or None: LightRAG's files but no marker
    passages: int | None
    failed: int | None
    bytes: int
    appends: int  # appends in its lineage
    appending_to: str | None = None
    pinned_by: tuple[str, ...] = ()  # releases pinning what it holds
    marker: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def snapshot(self) -> str | None:
        """The snapshot to name it by: the one an append in progress is heading for."""
        return self.appending_to or self.procedures_ref


def list_stores(root: Path, env: Mapping[str, str] = os.environ, *, extra: Iterable[Path] = ()) -> list[StoreInfo]:
    """Every ``lightrag_store_*`` folder under ``root``, the one ``CFR_LIGHTRAG_WORKDIR`` names,
    and ``extra``; only folders that are stores."""
    folders = sorted(root.glob(f"{STORE_PREFIX}*"))
    if env.get(WORKDIR_ENV):
        folders.append(root / env[WORKDIR_ENV])
    folders += list(extra)
    pins = pinned_snapshots(root / "registry")
    found, seen = [], set()
    for folder in folders:
        key = folder.resolve()
        if key in seen or not is_store(folder):
            continue
        seen.add(key)
        marker = read_marker(folder) or {}
        ref = marker.get("procedures_ref")
        found.append(
            StoreInfo(
                folder=folder,
                procedures_ref=ref,
                state=state(marker) if marker else None,
                passages=marker.get("passages"),
                failed=marker.get("failed"),
                bytes=size(folder),
                appends=sum(1 for step in marker.get("lineage") or () if step.get("made") == "append"),
                appending_to=marker.get("appending_to"),
                pinned_by=tuple(pins.get(ref, ())) if ref else (),
                marker=marker,
            )
        )
    return found


def removable(info: StoreInfo, env: Mapping[str, str] = os.environ, root: Path = Path()) -> str | None:
    """Why a store can't be removed, or None when it can."""
    if info.pinned_by:
        return f"pinned by {', '.join(info.pinned_by)}"
    if env.get(WORKDIR_ENV) and info.folder.resolve() == (root / env[WORKDIR_ENV]).resolve():
        return f"{WORKDIR_ENV} names it"
    return None


def override_hint(procedures_ref: str, env: Mapping[str, str] = os.environ) -> str:
    """What to do when ``CFR_LIGHTRAG_WORKDIR`` points at a store built from another snapshot."""
    if not env.get(WORKDIR_ENV):
        return ""
    return f"; {WORKDIR_ENV} chooses this folder: unset it to use {store_name(procedures_ref)}"
