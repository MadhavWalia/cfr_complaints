"""The provisioned dashboard: it can't be loaded here, so check what would silently show nothing."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2] / "docker/observability/grafana/provisioning"
DASHBOARD = json.loads((ROOT / "dashboards/cfr-overview.json").read_text())
PANELS = [p for p in DASHBOARD["panels"] if p["type"] != "row"]


def test_every_panel_uses_a_provisioned_datasource():
    provisioned = {d["uid"] for d in yaml.safe_load((ROOT / "datasources/tempo.yaml").read_text())["datasources"]}
    assert {t["datasource"]["uid"] for p in PANELS for t in p["targets"]} == provisioned


def test_ids_are_unique_and_panels_stay_on_the_grid():
    ids = [p["id"] for p in DASHBOARD["panels"]]
    assert len(ids) == len(set(ids))
    assert all(p["gridPos"]["x"] + p["gridPos"]["w"] <= 24 for p in DASHBOARD["panels"])


def test_it_shows_all_three_signals():
    assert {t["datasource"]["type"] for p in PANELS for t in p["targets"]} == {"prometheus", "loki", "tempo"}


def test_span_searches_list_spans_so_durations_are_the_spans_own():
    """As a trace table, a flush search showed each flush's whole batch: 30 minutes, not 30 seconds."""
    for panel in PANELS:
        for target in panel["targets"]:
            if target.get("queryType") == "traceql" and "procedures.lightrag.batch" not in target["query"]:
                assert target["tableType"] == "spans", panel["title"]


def test_retries_come_from_the_metric_not_from_log_text():
    (panel,) = [p for p in PANELS if p["title"].startswith("Retries")]
    assert "cfr_llm_retries" in panel["targets"][0]["expr"]
