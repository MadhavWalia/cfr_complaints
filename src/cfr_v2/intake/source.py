"""Case sources and the V1 field contract (OQ-21).

A source yields one mapping per case with the columns of ``sql/current_cases.sql``.
Field use matches V1 exactly: models see ``case_no``, the redacted ``msg_tx2``,
``prim_ctgy`` and ``prim_subctgy``; ``latest_rslv_optn`` is the only evaluation
label; every other column is stored (RESTRICTED) and unused.

With the LUMI baseline (LDR-0014) the query also returns ``BASELINE_COLUMNS``; they
are split off before the source record is built (see ``intake/baseline.py``).
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime
from importlib import resources
from pathlib import Path
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

# Bump on every change to sql/lumi_baseline.sql; recorded on each BaselineProfile.
BASELINE_QUERY_VERSION = "lumi-baseline-1"
# Added by the combined query (case_query_sql(with_baseline=True)).
BASELINE_COLUMNS = ("baseline_llm_context", "baseline_ref_map", "baseline_n_accounts")
# Added by the source, not the query: when the baseline query ran (the job start).
BASELINE_OBSERVED_AT_COLUMN = "baseline_observed_at"
_NOT_SOURCE = frozenset((*BASELINE_COLUMNS, BASELINE_OBSERVED_AT_COLUMN))

_BASELINE_FINAL_SELECT = """SELECT
  c.*,
  bl.llm_context AS baseline_llm_context,
  bl.ref_map AS baseline_ref_map,
  bl.n_accounts AS baseline_n_accounts
FROM cases c
LEFT JOIN baseline bl ON bl.case_no = c.case_no"""


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


def _packaged_sql(name: str) -> str:
    return resources.files("cfr_v2.intake").joinpath(f"sql/{name}").read_text(encoding="utf-8").strip().rstrip(";")


# The only place the case query selects cases by date. ``by_case_no`` swaps it for a list, so the
# rest (US current records, excluded groups, transcript matching, baseline) stays as in production.
_DATE_FILTER = "AND DATE(orig_rslv_ts) BETWEEN @start_date AND @end_date"
_CASE_NO_FILTER = "AND CAST(case_no AS STRING) IN UNNEST(@case_nos)"


def case_query_sql(*, with_baseline: bool = False, by_case_no: bool = False) -> str:
    """The packaged V2 case query, ready for parameter binding.

    ``with_baseline`` appends the LUMI baseline (LDR-0014) to the same WITH clause, so
    cases and baseline come from one BigQuery job: the case query's final SELECT becomes
    the ``cases`` CTE, the baseline reads its ``base_cases`` CTE, and the result is
    ``cases`` left-joined to ``baseline`` (a case without a match gets NULL columns).

    ``by_case_no`` selects cases by ``@case_nos`` (an ARRAY<STRING>) instead of the
    resolution-date window; everything else is the same query.
    """
    sql = _packaged_sql("current_cases.sql")
    if by_case_no:
        if sql.count(_DATE_FILTER) != 1:
            raise RuntimeError("current_cases.sql: the date filter is not where expected")
        sql = sql.replace(_DATE_FILTER, _CASE_NO_FILTER)
    if not with_baseline:
        return sql
    cut = sql.rindex("\nSELECT\n")
    ctes, final = sql[:cut], sql[cut + 1 :]
    if not final.startswith("SELECT\ncase_no"):
        raise RuntimeError("current_cases.sql: final SELECT not found where expected")
    return f"{ctes},\n\ncases AS (\n{final}\n),\n\n{_packaged_sql('lumi_baseline.sql')}\n\n{_BASELINE_FINAL_SELECT}"


def case_number(value: Any) -> str | None:
    """A case number as the query compares it (a string), from a spreadsheet or CSV cell.
    Excel stores numbers as floats, so ``1234567.0`` becomes ``"1234567"``. Blank is None."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text or None


def read_case_numbers(path: Path, *, column: str = CASE_ID_COLUMN, sheet: str | None = None) -> list[str]:
    """Case numbers from a column of an .xlsx or .csv file, blanks skipped, each once, in order.
    The column is found by its header, ignoring case and surrounding spaces."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        book = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = iter((book[sheet] if sheet else book.active).iter_rows(values_only=True))
            header = next(rows, ())
            cells = _column(header, column, path)
            values = [row[cells] if cells < len(row) else None for row in rows]
        finally:
            book.close()
    elif path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            cells = _column(header, column, path)
            values = [row[cells] if cells < len(row) else None for row in reader]
    else:
        raise ValueError(f"{path.name}: expected an .xlsx or .csv file")
    numbers = [n for n in (case_number(v) for v in values) if n is not None]
    return list(dict.fromkeys(numbers))


def _column(header: Iterable[Any], column: str, path: Path) -> int:
    names = [str(h).strip().lower() if h is not None else "" for h in header]
    if column.strip().lower() not in names:
        raise ValueError(f"{path.name}: no {column!r} column in the first row")
    return names.index(column.strip().lower())


def baseline_columns(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The row's baseline columns, or None when the source did not query the baseline."""
    if not any(name in row for name in _NOT_SOURCE):
        return None
    return {name: row.get(name) for name in _NOT_SOURCE}


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
    """Canonical JSON of the case row, so a re-fetched identical row has the same ref.

    Baseline columns are left out: they are stored as their own artifacts, and the
    source record stays the ``current_cases.sql`` row.
    """
    try:
        return canonical_json({name: _source_value(value) for name, value in row.items() if name not in _NOT_SOURCE})
    except (TypeError, ValueError) as exc:
        column = next((n for n, v in row.items() if n not in _NOT_SOURCE and not _serializable(v)), "?")
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
