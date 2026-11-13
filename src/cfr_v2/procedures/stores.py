"""Where a snapshot's LightRAG store lives (LDR-0021): one folder per snapshot, named after it.

A store holds the passages of one snapshot only (LightRAG never removes a passage, so two would
mix), and records that snapshot in ``cfr_store.json``. Naming the folder after the snapshot lets
everything that searches find the store from the ref alone: the worker from the release's
``procedure_snapshot``, the CLIs from the ref they are given. ``CFR_LIGHTRAG_WORKDIR`` overrides
the folder, for a store kept somewhere else.

Light on purpose: no LightRAG import, so the console can use it.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

WORKDIR_ENV = "CFR_LIGHTRAG_WORKDIR"
STORE_MARKER = "cfr_store.json"  # in the store: the snapshot it was built from
STORE_PREFIX = "lightrag_store_"


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


def built_from(workdir: Path) -> str | None:
    """The snapshot a store records it was built from; None when it records none (or is missing)."""
    try:
        marker = json.loads((workdir / STORE_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return marker.get("procedures_ref") if isinstance(marker, dict) else None


def override_hint(procedures_ref: str, env: Mapping[str, str] = os.environ) -> str:
    """What to do when ``CFR_LIGHTRAG_WORKDIR`` points at a store built from another snapshot."""
    if not env.get(WORKDIR_ENV):
        return ""
    return f"; {WORKDIR_ENV} chooses this folder: unset it to use {store_name(procedures_ref)}"
