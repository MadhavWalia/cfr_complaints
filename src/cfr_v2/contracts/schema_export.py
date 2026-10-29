"""Export JSON Schemas for every contract.

    python -m cfr_v2.contracts.schema_export <out_dir>

Snapshot tests compare against these to catch accidental breaking changes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from cfr_v2 import contracts
from cfr_v2.contracts import Contract


def contract_models() -> dict[str, type[Contract]]:
    models = {}
    for name in contracts.__all__:
        obj = getattr(contracts, name)
        if isinstance(obj, type) and issubclass(obj, Contract) and obj is not Contract:
            models[name] = obj
    return dict(sorted(models.items()))


def export_schemas() -> dict[str, dict]:
    return {
        name: {"schema_version": model.SCHEMA_VERSION, "schema": model.model_json_schema()}
        for name, model in contract_models().items()
    }


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "schemas")
    out.mkdir(parents=True, exist_ok=True)
    for name, payload in export_schemas().items():
        (out / f"{name}.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
