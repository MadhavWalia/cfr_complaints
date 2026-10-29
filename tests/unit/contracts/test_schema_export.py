from __future__ import annotations

import json

from cfr_v2.contracts.schema_export import contract_models, export_schemas, main


def test_every_exported_contract_has_a_schema():
    schemas = export_schemas()
    assert set(schemas) == set(contract_models())
    for payload in schemas.values():
        assert payload["schema_version"] >= 1
        assert payload["schema"]["type"] == "object"


def test_main_writes_one_file_per_contract(tmp_path):
    assert main(["schema_export", str(tmp_path)]) == 0
    files = sorted(p.stem for p in tmp_path.glob("*.json"))
    assert files == sorted(contract_models())
    json.loads((tmp_path / "TaskEnvelope.json").read_text())
