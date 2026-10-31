from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from cfr_v2.intake import (
    EVALUATION_LABELS,
    MODEL_VISIBLE_METADATA,
    SOURCE_COLUMNS,
    InvalidSourceRowError,
    case_query_sql,
)
from cfr_v2.intake.fakes import synthetic_row
from cfr_v2.intake.source import (
    case_metadata,
    evaluation_labels,
    source_case_id,
    source_record_bytes,
)


def _final_select(sql: str) -> str:
    return sql[sql.rindex("SELECT") :]


def test_potl_defect_filter_removed():
    sql = case_query_sql()
    assert "IS DISTINCT FROM 'Yes'" not in sql
    assert "@start_date" in sql and "@end_date" in sql
    assert not sql.endswith(";")


def test_query_outputs_every_source_column():
    final = _final_select(case_query_sql())
    for column in SOURCE_COLUMNS:
        assert re.search(rf"\b{column}\b", final), column


def test_field_contract_matches_v1():
    # OQ-21: models see only these; latest_rslv_optn is the only label.
    assert MODEL_VISIBLE_METADATA == ("prim_ctgy", "prim_subctgy")
    assert EVALUATION_LABELS == ("latest_rslv_optn",)
    row = synthetic_row(1)
    assert set(case_metadata(row)) == {"prim_ctgy", "prim_subctgy"}
    assert evaluation_labels(row) == {"latest_rslv_optn": row["latest_rslv_optn"]}


@pytest.mark.parametrize("value", [None, "", "  ", float("nan")])
def test_missing_case_no(value):
    with pytest.raises(InvalidSourceRowError) as exc:
        source_case_id({"case_no": value})
    assert exc.value.reason == "MISSING_CASE_NO"


def test_numeric_case_no_becomes_text():
    assert source_case_id({"case_no": 12345}) == "12345"


def test_source_record_is_canonical():
    row = synthetic_row(2)
    reordered = dict(reversed(list(row.items())))
    assert source_record_bytes(row) == source_record_bytes(reordered)


def test_unserializable_row_rejected_without_values():
    with pytest.raises(InvalidSourceRowError) as exc:
        source_record_bytes({"case_no": "1", "blob": object()})
    assert exc.value.reason == "UNSERIALIZABLE_ROW:blob:TypeError"  # column name, never its value


def test_nan_rejected_naming_the_column():
    with pytest.raises(InvalidSourceRowError) as exc:
        source_record_bytes({"case_no": "1", "cm11": float("nan")})
    assert exc.value.reason == "UNSERIALIZABLE_ROW:cm11:ValueError"


def test_bigquery_datetime_kept_as_wall_clock():
    """DATETIME columns come back naive; stored as written, no zone assumed."""
    naive = {"case_no": "1", "orig_rslv_ts": datetime(2026, 9, 1, 10, 30)}
    aware = {"case_no": "1", "orig_rslv_ts": datetime(2026, 9, 1, 10, 30, tzinfo=UTC)}
    assert b'"orig_rslv_ts":"2026-09-01T10:30:00"' in source_record_bytes(naive)
    assert b'"orig_rslv_ts":"2026-09-01T10:30:00+00:00"' in source_record_bytes(aware)
