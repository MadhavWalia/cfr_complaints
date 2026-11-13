"""Each snapshot's LightRAG store has its own folder, named after it (LDR-0021)."""

from __future__ import annotations

import json

import pytest

from cfr_v2.procedures.stores import STORE_MARKER, built_from, override_hint, store_dir, store_name

REF = "sha256:b72453c4ef4cae8c944af6bacd14fcdfefab011b904de80f22669a9fae47aa8b"


def test_the_folder_is_named_after_the_snapshot_unless_overridden(tmp_path):
    assert store_name(REF) == "lightrag_store_b72453c4ef4c"
    assert store_dir(REF, root=tmp_path, env={}) == tmp_path / "lightrag_store_b72453c4ef4c"
    assert store_dir(REF, root=tmp_path, env={"CFR_LIGHTRAG_WORKDIR": "elsewhere"}) == tmp_path / "elsewhere"
    assert store_dir(REF, root=tmp_path, env={"CFR_LIGHTRAG_WORKDIR": "/abs/store"}).as_posix() == "/abs/store"
    for bad in ("sha256:../x", "sha256:", "a/b"):
        with pytest.raises(ValueError, match="not a snapshot ref"):
            store_name(bad)


def test_a_store_records_its_snapshot(tmp_path):
    assert built_from(tmp_path / "missing") is None and built_from(tmp_path) is None
    (tmp_path / STORE_MARKER).write_text("not json")
    assert built_from(tmp_path) is None
    (tmp_path / STORE_MARKER).write_text(json.dumps({"procedures_ref": REF}))
    assert built_from(tmp_path) == REF
    assert override_hint(REF, {}) == ""
    assert override_hint(REF, {"CFR_LIGHTRAG_WORKDIR": "x"}).endswith("unset it to use lightrag_store_b72453c4ef4c")
