"""LUMI baseline at intake (LDR-0014): combined query, parsing, artifacts and request fields."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest
import sqlglot

from cfr_v2.artifacts import InMemoryArtifactStore
from cfr_v2.contracts import (
    AuthorizationContext,
    BaselineAccount,
    BaselineProfile,
    BaselineRefMap,
    Classification,
    IngestionRequest,
    Lane,
    canonical_hash,
)
from cfr_v2.intake import (
    BASELINE_QUERY_VERSION,
    SOURCE_COLUMNS,
    BaselineStatus,
    ProfanityFilter,
    case_query_sql,
    parse_baseline,
    prepare_case,
)
from cfr_v2.intake.fakes import OBSERVED_AT, RegexMasker, synthetic_names, synthetic_row
from cfr_v2.intake.source import baseline_columns, source_record_bytes

NOW = datetime(2026, 11, 1, tzinfo=UTC)
READ_ALL = AuthorizationContext(
    principal="test", purpose="test", allowed_classifications=frozenset({"RESTRICTED", "INTERNAL"})
)
CONTEXT = {
    "case_received_date": "2026-08-30",
    "accounts": [
        {
            "account_ref": "A1",
            "is_case_account": True,
            "product": "Gold Card",
            "co_brand_partner": None,
            "business_unit": "Consumer",
            "account_type": "Card",
            "status_at_case": "Active",
            "opened_month": "2019-04",
            "card_replaced_month": None,
            "holder": "Customer",
            "card_role": "Basic",
            "role_detail": None,
            "market": "US",
        }
    ],
}
REF_MAP = [{"account_ref": "A1", "rel_id": 9001, "cm15": "371234567890123"}]


def _columns(context=CONTEXT, ref_map=REF_MAP, observed_at=OBSERVED_AT):
    return {
        "baseline_llm_context": None if context is None else json.dumps(context),
        "baseline_ref_map": ref_map,
        "baseline_n_accounts": None if context is None else len(context["accounts"]),
        "baseline_observed_at": observed_at,
    }


# --- combined query --------------------------------------------------------------------


def test_combined_query_is_one_with_clause_reading_base_cases():
    sql = case_query_sql(with_baseline=True)
    code = re.sub(r"/\*.*?\*/|--[^\n]*", "", sql, flags=re.DOTALL)  # comments may mention WITH
    assert len(re.findall(r"\bWITH\b", code)) == 1 and "{{" not in code
    assert "cases AS (\nSELECT\ncase_no" in sql
    assert "baseline AS (" in sql
    assert re.search(r"bl_base AS \(.*?FROM base_cases", sql, re.DOTALL)  # not the transcript-joined rows
    assert sql.rstrip().endswith("LEFT JOIN baseline bl ON bl.case_no = c.case_no")
    assert "@start_date" in sql and not sql.endswith(";")
    assert sql.startswith(case_query_sql()[: case_query_sql().rindex("\nSELECT\n")])  # case CTEs unchanged


def test_case_columns_still_come_from_the_case_query():
    sql = case_query_sql(with_baseline=True)
    cases = sql[sql.index("cases AS (") : sql.index("bl_params AS (")]
    for column in SOURCE_COLUMNS:
        assert re.search(rf"\b{column}\b", cases), column


def test_llm_context_fields_are_exactly_the_contract_allowlist():
    sql = case_query_sql(with_baseline=True)
    struct = re.search(r"ARRAY_AGG\(STRUCT\((.*?)\) ORDER BY ref_no\) AS accounts", sql, re.DOTALL).group(1)
    fields = {f.strip() for f in struct.split(",")}
    assert fields == set(BaselineAccount.model_fields)


def test_baseline_query_selects_no_protected_attributes():
    baseline_sql = case_query_sql(with_baseline=True).split("bl_params AS (", 1)[1].lower()
    for column in ("gend_cd", "mar_sta", "dob", "birth_yr", "birth_plce", "birth_ctry", "annual_incom"):
        assert column not in baseline_sql


@pytest.mark.parametrize("with_baseline", [False, True])
def test_query_parses_as_bigquery_sql(with_baseline):
    """Catches syntax slips the structural checks miss (e.g. a trailing comma after the WITH clause)."""
    tree = sqlglot.parse_one(case_query_sql(with_baseline=with_baseline), read="bigquery")
    names = [c.alias_or_name for c in tree.selects]
    if with_baseline:
        assert names == ["*", "baseline_llm_context", "baseline_ref_map", "baseline_n_accounts"]
    else:
        assert names == list(SOURCE_COLUMNS)


# --- parsing ---------------------------------------------------------------------------


def test_present_baseline_parses_into_profile_and_ref_map():
    parsed = parse_baseline(_columns(), query_version="v")
    assert parsed.status is BaselineStatus.PRESENT
    assert parsed.observed_at == OBSERVED_AT
    assert parsed.profile.accounts[0].product == "Gold Card"
    assert parsed.profile.query_version == "v"
    assert parsed.ref_map.entries[0].rel_id == "9001"  # numeric ids become text


def test_no_match_is_absent_and_no_columns_is_not_queried():
    absent = parse_baseline(_columns(context=None, ref_map=None), query_version="v")
    assert (absent.status, absent.observed_at, absent.profile) == (BaselineStatus.ABSENT, OBSERVED_AT, None)
    assert parse_baseline(None, query_version="v").status is BaselineStatus.NOT_QUERIED


@pytest.mark.parametrize(
    "columns",
    [
        _columns() | {"baseline_llm_context": "{not json"},
        _columns(context={**CONTEXT, "accounts": [{**CONTEXT["accounts"][0], "gend_cd": "F"}]}),  # outside allowlist
        _columns(context={**CONTEXT, "accounts": []}),
        _columns(ref_map=[{"account_ref": "A2", "rel_id": 1, "cm15": None}]),  # refs disagree
        _columns(ref_map=None),
        _columns(observed_at=datetime(2026, 9, 1)),  # naive
    ],
)
def test_unusable_baseline_is_invalid_and_nothing_is_kept(columns):
    parsed = parse_baseline(columns, query_version="v")
    assert parsed.status is BaselineStatus.INVALID
    assert parsed.profile is None and parsed.ref_map is None


def test_profile_rejects_two_case_accounts():
    account = BaselineAccount(account_ref="A1", is_case_account=True)
    with pytest.raises(ValueError):
        BaselineProfile(query_version="v", accounts=(account, account.model_copy(update={"account_ref": "A2"})))


# --- source record, artifacts and request ----------------------------------------------


def test_source_record_excludes_the_baseline_columns():
    with_bl, without = synthetic_row(0), synthetic_row(0, baseline=False)
    assert baseline_columns(with_bl) is not None and baseline_columns(without) is None
    assert source_record_bytes(with_bl) == source_record_bytes(without)


def _prepare(row):
    store = InMemoryArtifactStore()
    prepared = prepare_case(
        row,
        store=store,
        masker=RegexMasker(synthetic_names()),
        profanity=ProfanityFilter(),
        lane=Lane.BATCH,
        received_at=NOW,
    )
    return store, prepared


def test_prepared_case_stores_profile_internal_and_ref_map_restricted():
    store, prepared = _prepare(synthetic_row(0))
    req = prepared.request
    assert prepared.baseline is BaselineStatus.PRESENT
    assert req.baseline_observed_at == OBSERVED_AT
    profile = BaselineProfile.model_validate_json(
        store.get(req.baseline_ref, classification=Classification.INTERNAL, authorization=READ_ALL)
    )
    ref_map = BaselineRefMap.model_validate_json(
        store.get(req.baseline_ref_map_ref, classification=Classification.RESTRICTED, authorization=READ_ALL)
    )
    assert profile.query_version == ref_map.query_version == BASELINE_QUERY_VERSION
    assert ref_map.entries[0].cm15 == synthetic_row(0)["cm15"]
    assert "cm15" not in profile.model_dump_json() and "rel_id" not in profile.model_dump_json()
    by_ref = {a.metadata.ref: a.metadata.classification for a in prepared.artifacts}
    assert by_ref[req.baseline_ref] is Classification.INTERNAL
    assert by_ref[req.baseline_ref_map_ref] is Classification.RESTRICTED


def test_case_without_match_is_admitted_without_refs():
    _, prepared = _prepare(synthetic_row(3))  # every seventh synthetic case has no match
    assert prepared.baseline is BaselineStatus.ABSENT
    assert prepared.request.baseline_ref is None and prepared.request.baseline_observed_at == OBSERVED_AT


def test_observation_time_does_not_change_the_content_hash():
    _, first = _prepare(synthetic_row(0))
    _, later = _prepare(synthetic_row(0, baseline_observed_at=datetime(2026, 12, 1, tzinfo=UTC)))
    assert first.request.baseline_observed_at != later.request.baseline_observed_at
    assert first.request.content_hash() == later.request.content_hash()


def test_request_without_baseline_keeps_its_pre_baseline_hash():
    """Requests admitted before LDR-0014 must still match on re-run (no spurious 'content differs')."""
    _, prepared = _prepare(synthetic_row(0, baseline=False))
    req = prepared.request
    legacy = req.model_dump(
        mode="json", exclude={"received_at", "lane", "baseline_ref", "baseline_ref_map_ref", "baseline_observed_at"}
    )
    assert req.content_hash() == canonical_hash(legacy)


def test_refs_without_observation_time_are_rejected():
    _, prepared = _prepare(synthetic_row(0))
    with pytest.raises(ValueError, match="baseline_observed_at"):
        IngestionRequest(**{**prepared.request.model_dump(), "baseline_observed_at": None})
