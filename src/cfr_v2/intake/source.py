"""Case sources and the V1 field contract (OQ-21).

A source yields one mapping per case with the columns of ``sql/current_cases.sql``.
Field use matches V1 exactly: models see ``case_no``, the redacted ``msg_tx2``,
``prim_ctgy`` and ``prim_subctgy``; ``latest_rslv_optn`` is the only evaluation
label; every other column is stored (RESTRICTED) and unused.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime
from importlib import resources
from typing import Any, Protocol

from pydantic import JsonValue

from cfr_v2.contracts import canonical_json

SOURCE_SYSTEM = "icare"
CASE_ID_COLUMN = "case_no"
TRANSCRIPT_COLUMN = "msg_tx2"
MODEL_VISIBLE_METADATA = ("prim_ctgy", "prim_subctgy")
EVALUATION_LABELS = ("latest_rslv_optn",)
SOURCE_INTERACTION_IDS_COLUMN = "intrct_ids"
LINKAGE_QUALITY_COLUMN = "exact_match_flag"

# Output columns of sql/current_cases.sql, in order.
SOURCE_COLUMNS = (
    "case_no",
    "latest_rslv_optn",
    "discrimination_type",
    "orig_rslv_ts",
    "reg_sta",
    "prim_ctgy",
    "prim_subctgy",
    "cqat_flag",
    "pie_flag",
    "creat_opr_id",
    "cm11",
    "cm15",
    "case_note",
    "case_ds",
    "ccp_note",
    "pend_resp_note",
    "req_note",
    "pend_req_note",
    "latest_rslv_note",
    "rcpt_ts",
    "emp_ntwk_id",
    "potl_defect_flag",
    "err_type",
    "err_sub_type",
    "case_type",
    "intrct_ids",
    "doc_id",
    "exact_match_flag",
    "msg_tx2",
)


class InvalidSourceRowError(ValueError):
    """The row cannot be admitted; ``reason`` is safe to persist (no field values)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CaseSource(Protocol):
    def fetch(self, *, start_date: date, end_date: date) -> Iterator[Mapping[str, Any]]: ...


class RecordsCaseSource:
    """Rows already in memory (synthetic fixtures, a parquet read elsewhere, an API payload)."""

    def __init__(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self.rows = list(rows)

    def fetch(self, *, start_date: date, end_date: date) -> Iterator[Mapping[str, Any]]:
        return iter(self.rows)


def case_query_sql() -> str:
    """The packaged V2 case query, ready for parameter binding."""
    sql = resources.files("cfr_v2.intake").joinpath("sql/current_cases.sql").read_text(encoding="utf-8")
    return sql.strip().rstrip(";")


def source_case_id(row: Mapping[str, Any]) -> str:
    value = row.get(CASE_ID_COLUMN)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        raise InvalidSourceRowError("MISSING_CASE_NO")
    text = str(value).strip()
    if not text:
        raise InvalidSourceRowError("MISSING_CASE_NO")
    return text


def _scalar(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    return str(value)


def case_metadata(row: Mapping[str, Any]) -> dict[str, JsonValue]:
    return {name: _scalar(row.get(name)) for name in MODEL_VISIBLE_METADATA}


def evaluation_labels(row: Mapping[str, Any]) -> dict[str, JsonValue]:
    return {name: _scalar(row.get(name)) for name in EVALUATION_LABELS}


def _source_value(value: Any) -> Any:
    # BigQuery DATETIME arrives as a naive datetime: a wall-clock value with no zone.
    # Keep it as written rather than assume one (the canonical hasher rejects naive datetimes).
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.isoformat()
    return value


def source_record_bytes(row: Mapping[str, Any]) -> bytes:
    """Canonical JSON of the whole row, so a re-fetched identical row has the same ref."""
    try:
        return canonical_json({name: _source_value(value) for name, value in row.items()})
    except (TypeError, ValueError) as exc:
        column = next((name for name, value in row.items() if not _serializable(value)), "?")
        raise InvalidSourceRowError(f"UNSERIALIZABLE_ROW:{column}:{type(exc).__name__}") from None


def _serializable(value: Any) -> bool:
    try:
        canonical_json(_source_value(value))
    except (TypeError, ValueError):
        return False
    return True


def source_interaction_ids(row: Mapping[str, Any]) -> tuple[str, ...]:
    ids = row.get(SOURCE_INTERACTION_IDS_COLUMN) or ()
    if isinstance(ids, str):
        ids = (ids,)
    return tuple(sorted({str(i) for i in ids if i is not None}))
