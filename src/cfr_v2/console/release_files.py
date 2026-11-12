"""A new release file from the console (LDR-0025 step 3, user decision 2026-11-12).

The console copies an existing release's YAML and changes only its id and its routing pins (the
procedure snapshot and the knowledge document). Stage programs and versions are never edited here:
they follow the code (LDR-0022). The new file is a git change the user reviews and commits.
"""

from __future__ import annotations

import re
from pathlib import Path

from cfr_v2.registry.manifests import ReleaseRegistry

RELEASE_ID = re.compile(r"[a-z0-9][a-z0-9.-]{1,62}")
REF = re.compile(r"sha256:[0-9a-f]{64}")
PINS = ("procedure_snapshot", "routing_verticals")


class ReleaseFileError(ValueError):
    """A new release can't be written as asked; the message says what to change."""


def write_release(registry: Path, base: str, new: str, pins: dict[str, str | None]) -> Path:
    """``registry/releases/<new>.yaml``: ``base``'s file with ``release_id: new`` and ``pins`` set.
    Comments and everything else are kept. Refused when ``new`` exists or the result doesn't load."""
    if not RELEASE_ID.fullmatch(new):
        raise ReleaseFileError("Release id: lower-case letters, digits, dots and hyphens, such as cfr-v2-phase-f.")
    folder = registry / "releases"
    source, target = folder / f"{base}.yaml", folder / f"{new}.yaml"
    if not source.is_file():
        raise ReleaseFileError(f"Based on: no release {base}.")
    if target.exists():
        raise ReleaseFileError(f"Release id: {new} already exists. Choose another.")
    text = source.read_text(encoding="utf-8")
    for key, value in {"release_id": new, **pins}.items():
        if value is None:
            continue
        if key in PINS and not REF.fullmatch(value):
            raise ReleaseFileError(f"{key}: a full ref, sha256: and 64 hex characters.")
        line = re.compile(rf"^{key}:.*$", re.MULTILINE)
        text = line.sub(f"{key}: {value}", text, count=1) if line.search(text) else f"{text.rstrip()}\n{key}: {value}\n"
    text = re.sub(r"^checksum:.*\n?", "", text, flags=re.MULTILINE)  # the content changed: no declared checksum
    with target.open("x", encoding="utf-8") as out:  # never over an existing file
        out.write(text)
    try:
        ReleaseRegistry(registry).load(new)
    except Exception as exc:  # noqa: BLE001 - any problem: the file goes, and the form says why
        target.unlink()
        first = (str(exc).splitlines() or [type(exc).__name__])[0]
        raise ReleaseFileError(f"The new file wouldn't load ({first}); nothing was written.") from None
    return target
