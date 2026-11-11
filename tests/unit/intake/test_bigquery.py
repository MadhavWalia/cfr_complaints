from __future__ import annotations

import json
import sys
import types
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from cfr_v2.intake import BigQueryCaseSource, BigQuerySettings, case_query_sql


@pytest.fixture
def fake_google(monkeypatch):
    """Minimal stand-ins for google-cloud-bigquery / google-auth (not installable here)."""
    calls: dict = {}

    class QueryJobConfig:
        def __init__(self, query_parameters):
            self.query_parameters = query_parameters

    class ScalarQueryParameter:
        def __init__(self, name, type_, value):
            self.name, self.type_, self.value = name, type_, value

    class ArrayQueryParameter(ScalarQueryParameter):
        pass

    class Client:
        def __init__(self, project, credentials, client_options):
            calls["client"] = (project, credentials, client_options)

    class Credentials:
        @staticmethod
        def from_service_account_info(info):
            calls["info"] = info
            return "creds"

    class ClientOptions:
        def __init__(self, api_endpoint):
            self.api_endpoint = api_endpoint

    modules = {
        "google": types.ModuleType("google"),
        "google.cloud": types.ModuleType("google.cloud"),
        "google.cloud.bigquery": SimpleNamespace(
            QueryJobConfig=QueryJobConfig,
            ScalarQueryParameter=ScalarQueryParameter,
            ArrayQueryParameter=ArrayQueryParameter,
            Client=Client,
        ),
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.service_account": SimpleNamespace(Credentials=Credentials),
        "google.api_core": types.ModuleType("google.api_core"),
        "google.api_core.client_options": SimpleNamespace(ClientOptions=ClientOptions),
    }
    modules["google.cloud"].bigquery = modules["google.cloud.bigquery"]
    modules["google.oauth2"].service_account = modules["google.oauth2.service_account"]
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return calls


class _Row(dict):
    def items(self):
        return super().items()


class _FakeClient:
    def __init__(self, rows, started=None):
        self.rows, self.queries, self.started = rows, [], started

    def query(self, sql, job_config):
        self.queries.append((sql, job_config))
        return SimpleNamespace(result=lambda: iter(self.rows), started=self.started)


def test_settings_from_env_and_secret_not_in_repr():
    s = BigQuerySettings.from_env(
        {"GCP_PROJECT_ID": "p", "GCP_SERVICE_ACCOUNT_JSON": '{"private_key": "SECRET"}', "GCP_BQ_API_ENDPOINT": "https://bq"}
    )
    assert s.api_endpoint == "https://bq"
    assert "SECRET" not in repr(s)


def test_settings_require_project_and_credentials():
    with pytest.raises(ValueError, match="GCP_SERVICE_ACCOUNT_JSON"):
        BigQuerySettings.from_env({"GCP_PROJECT_ID": "p"})


def test_client_built_like_v1_prod(fake_google):
    settings = BigQuerySettings("proj", json.dumps({"type": "service_account"}), "https://bq.internal")
    source = BigQueryCaseSource(settings)
    assert source.client is not None
    project, creds, options = fake_google["client"]
    assert (project, creds, options.api_endpoint) == ("proj", "creds", "https://bq.internal")
    assert fake_google["info"] == {"type": "service_account"}


def test_invalid_service_account_json_is_not_echoed(fake_google):
    source = BigQueryCaseSource(BigQuerySettings("proj", "not-json SECRET"))
    with pytest.raises(ValueError) as exc:
        source.client
    assert "SECRET" not in str(exc.value)


def test_fetch_binds_date_window_and_yields_dicts(fake_google):
    client = _FakeClient([_Row(case_no="1", msg_tx2="hi")])
    source = BigQueryCaseSource(client=client, with_baseline=False)
    rows = list(source.fetch(start_date=date(2026, 9, 1), end_date=date(2026, 9, 7)))
    assert rows == [{"case_no": "1", "msg_tx2": "hi"}]
    sql, job_config = client.queries[0]
    assert sql == case_query_sql()
    assert [(p.name, p.type_, p.value) for p in job_config.query_parameters] == [
        ("start_date", "DATE", date(2026, 9, 1)),
        ("end_date", "DATE", date(2026, 9, 7)),
    ]


def test_fetch_with_baseline_runs_the_combined_query_and_stamps_the_job_start(fake_google):
    started = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
    client = _FakeClient([_Row(case_no="1"), _Row(case_no="2")], started=started)
    rows = list(BigQueryCaseSource(client=client).fetch(start_date=date(2026, 9, 1), end_date=date(2026, 9, 7)))
    assert client.queries[0][0] == case_query_sql(with_baseline=True)  # one job for cases and baseline
    assert [r["baseline_observed_at"] for r in rows] == [started, started]


def test_observed_at_falls_back_to_submission_time(fake_google):
    rows = list(BigQueryCaseSource(client=_FakeClient([_Row(case_no="1")])).fetch(start_date=date(2026, 9, 1), end_date=date(2026, 9, 1)))
    assert rows[0]["baseline_observed_at"].tzinfo is not None


def test_fetch_by_case_numbers_binds_the_list_and_runs_the_same_query(fake_google):
    client = _FakeClient([_Row(case_no="1234567")])
    source = BigQueryCaseSource(client=client, case_nos=["1234567", "7654321"])
    rows = list(source.fetch(start_date=date(2026, 9, 1), end_date=date(2026, 9, 1)))  # dates ignored
    assert rows[0]["case_no"] == "1234567"
    sql, job_config = client.queries[0]
    assert sql == case_query_sql(with_baseline=True, by_case_no=True)
    assert [(p.name, p.type_, p.value) for p in job_config.query_parameters] == [
        ("case_nos", "STRING", ["1234567", "7654321"])
    ]


@pytest.mark.parametrize("with_baseline", [False, True])
def test_the_case_number_query_differs_only_in_how_cases_are_selected(with_baseline):
    by_date = case_query_sql(with_baseline=with_baseline)
    by_number = case_query_sql(with_baseline=with_baseline, by_case_no=True)
    assert "IN UNNEST(@case_nos)" in by_number and "BETWEEN @start_date AND @end_date" not in by_number
    assert (
        by_number.replace(
            "AND CAST(case_no AS STRING) IN UNNEST(@case_nos)",
            "AND DATE(orig_rslv_ts) BETWEEN @start_date AND @end_date",
        )
        == by_date
    )
