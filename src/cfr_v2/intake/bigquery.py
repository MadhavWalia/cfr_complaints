"""BigQuery case source: port of V1 ``src/sql/fetch_client_prod.py`` + ``fetch_current.py``.

``google-cloud-bigquery`` is an enterprise-only dependency, imported lazily so
the rest of C01 runs and tests without it.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from cfr_v2.telemetry import get_logger, span

from .source import BASELINE_OBSERVED_AT_COLUMN, case_query_sql

logger = get_logger(__name__)


@dataclass(frozen=True)
class BigQuerySettings:
    project_id: str
    # Service-account key JSON (inline, as in V1). A secret: never logged or repr'd.
    service_account_json: str = field(repr=False)
    api_endpoint: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> BigQuerySettings:
        missing = [k for k in ("GCP_PROJECT_ID", "GCP_SERVICE_ACCOUNT_JSON") if not env.get(k)]
        if missing:
            raise ValueError(f"BigQuery settings missing: {', '.join(missing)}")
        return cls(
            project_id=env["GCP_PROJECT_ID"],
            service_account_json=env["GCP_SERVICE_ACCOUNT_JSON"],
            api_endpoint=env.get("GCP_BQ_API_ENDPOINT") or None,
        )


def make_client(settings: BigQuerySettings) -> Any:
    from google.api_core.client_options import ClientOptions
    from google.cloud import bigquery
    from google.oauth2 import service_account

    try:
        info = json.loads(settings.service_account_json)
    except json.JSONDecodeError:
        raise ValueError("GCP_SERVICE_ACCOUNT_JSON is not valid JSON") from None
    credentials = service_account.Credentials.from_service_account_info(info)
    options = ClientOptions(api_endpoint=settings.api_endpoint) if settings.api_endpoint else None
    return bigquery.Client(project=settings.project_id, credentials=credentials, client_options=options)


class BigQueryCaseSource:
    """Runs the packaged case query for a resolution-date window.

    With ``with_baseline`` (the default, LDR-0014) the LUMI baseline comes from the same
    job, and every row is stamped with the job's start time as ``baseline_observed_at``.
    """

    def __init__(
        self,
        settings: BigQuerySettings | None = None,
        *,
        client: Any = None,
        sql: str | None = None,
        with_baseline: bool = True,
    ) -> None:
        if client is None and settings is None:
            raise ValueError("BigQueryCaseSource needs settings or a client")
        self._settings = settings
        self._client = client
        self.with_baseline = with_baseline
        self.sql = sql or case_query_sql(with_baseline=with_baseline)

    @property
    def client(self) -> Any:
        if self._client is None:
            assert self._settings is not None
            self._client = make_client(self._settings)
        return self._client

    def fetch(self, *, start_date: date, end_date: date) -> Iterator[Mapping[str, Any]]:
        from google.cloud import bigquery

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
                bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
            ]
        )
        logger.info("bigquery_fetch start_date=%s end_date=%s baseline=%s", start_date, end_date, self.with_baseline)
        # Row iteration uses the REST API, like V1's create_bqstorage_client=False: the
        # gRPC storage API ignores the corporate trust store.
        with span("intake.fetch", window_start=start_date.isoformat(), window_end=end_date.isoformat()):
            submitted = datetime.now(UTC)
            job = self.client.query(self.sql, job_config=job_config)
            result = job.result()
        started = getattr(job, "started", None)
        observed_at = started if isinstance(started, datetime) and started.tzinfo else submitted
        for row in result:
            values = dict(row.items())
            if self.with_baseline:
                values[BASELINE_OBSERVED_AT_COLUMN] = observed_at
            yield values
