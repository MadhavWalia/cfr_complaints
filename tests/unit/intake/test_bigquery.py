from __future__ import annotations

import json
import sys
import types
from datetime import date
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
            QueryJobConfig=QueryJobConfig, ScalarQueryParameter=ScalarQueryParameter, Client=Client
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
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def query(self, sql, job_config):
        self.queries.append((sql, job_config))
        return SimpleNamespace(result=lambda: iter(self.rows))


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
    rows = list(BigQueryCaseSource(client=client).fetch(start_date=date(2026, 9, 1), end_date=date(2026, 9, 7)))
    assert rows == [{"case_no": "1", "msg_tx2": "hi"}]
    sql, job_config = client.queries[0]
    assert sql == case_query_sql()
    assert [(p.name, p.type_, p.value) for p in job_config.query_parameters] == [
        ("start_date", "DATE", date(2026, 9, 1)),
        ("end_date", "DATE", date(2026, 9, 7)),
    ]
