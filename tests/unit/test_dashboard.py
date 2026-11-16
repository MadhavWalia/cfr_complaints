"""The provisioned dashboards: they can't be loaded here, so check what would silently show nothing."""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parents[2]
ROOT = REPO / "docker/observability/grafana/provisioning"
NAMES = ("cfr-overview", "cfr-pipeline")
DASHBOARDS = {name: json.loads((ROOT / f"dashboards/{name}.json").read_text()) for name in NAMES}


def _panels(name):
    return [p for p in DASHBOARDS[name]["panels"] if p["type"] != "row"]


def _targets(name):
    return [(p, t) for p in _panels(name) for t in p["targets"]]


@pytest.mark.parametrize("name", NAMES)
def test_every_panel_uses_a_provisioned_datasource(name):
    provisioned = {d["uid"] for d in yaml.safe_load((ROOT / "datasources/tempo.yaml").read_text())["datasources"]}
    assert {t["datasource"]["uid"] for _, t in _targets(name)} == provisioned


@pytest.mark.parametrize("name", NAMES)
def test_ids_are_unique_and_panels_stay_on_the_grid_without_overlapping(name):
    panels = DASHBOARDS[name]["panels"]
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids))
    assert all(p["gridPos"]["x"] + p["gridPos"]["w"] <= 24 for p in panels)
    cells = {}
    for p in panels:
        g = p["gridPos"]
        for cell in itertools.product(range(g["x"], g["x"] + g["w"]), range(g["y"], g["y"] + g["h"])):
            assert cell not in cells, f"{p['title']} overlaps {cells[cell]}"
            cells[cell] = p["title"]


@pytest.mark.parametrize("name", NAMES)
def test_it_shows_all_three_signals(name):
    assert {t["datasource"]["type"] for _, t in _targets(name)} == {"prometheus", "loki", "tempo"}


@pytest.mark.parametrize("name", NAMES)
def test_span_searches_list_spans_so_durations_are_the_spans_own(name):
    """As a trace table, a flush search showed each flush's whole batch: 30 minutes, not 30 seconds.
    A case run's trace starts at its admission, so it would show days of waiting in the queue."""
    for panel, target in _targets(name):
        if target.get("queryType") == "traceql" and "procedures.lightrag.batch" not in target["query"]:
            assert target["tableType"] == "spans", panel["title"]


def test_retries_come_from_the_metric_not_from_log_text():
    (panel,) = [p for p in _panels("cfr-overview") if p["title"].startswith("Retries")]
    assert "cfr_llm_retries" in panel["targets"][0]["expr"]


def _defined_metrics() -> set[str]:
    """Every metric the code creates, as Prometheus names it (dots to underscores)."""
    found = set()
    for path in (REPO / "src/cfr_v2").rglob("*.py"):
        found |= set(re.findall(r'create_\w+\(\s*"(cfr\.[a-z_.]+)"', path.read_text(encoding="utf-8")))
    return {name.replace(".", "_") for name in found}


@pytest.mark.parametrize("name", NAMES)
def test_every_metric_a_panel_queries_is_one_the_code_records(name):
    defined = _defined_metrics()
    assert "cfr_llm_calls" in defined and "cfr_queue_depth" in defined  # the scan itself works
    for panel, target in _targets(name):
        for metric in re.findall(r"cfr_[a-z_]+?(?=\(|\"|\{|_bucket|_sum|_count|\s)", target.get("expr", "")):
            assert metric in defined, f"{panel['title']}: {metric}"


def test_the_queue_gauge_is_never_summed_across_processes():
    """Every process running the runtime reports the same database-wide queue depth."""
    for panel, target in _targets("cfr-pipeline"):
        expr = target.get("expr", "")
        if "cfr_queue_depth" in expr:
            assert not re.search(r"sum[^(]*\(\{__name__=~\"cfr_queue_depth", expr), panel["title"]


def test_the_two_dashboards_link_to_each_other():
    assert DASHBOARDS["cfr-overview"]["links"][0]["url"] == f"/d/{DASHBOARDS['cfr-pipeline']['uid']}"
    assert DASHBOARDS["cfr-pipeline"]["links"][0]["url"] == f"/d/{DASHBOARDS['cfr-overview']['uid']}"
