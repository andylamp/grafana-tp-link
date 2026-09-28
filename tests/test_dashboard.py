# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Check dashboard portability and the exporter measurement contract."""

from __future__ import annotations

import json
import re
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

JSON = dict[str, Any]
DEVICE_UNITS = {
    "current_consumption": "watt",
    "current_voltage": "volt",
    "current_current": "amp",
    "current_rssi": "dBm",
    "current_consumption_today": "watth",
    "current_month_consumption": "watth",
}
INFRASTRUCTURE_METRICS = {
    "up",
    "tapo_discovered_devices",
    "scrape_duration_seconds",
    "scrape_samples_scraped",
    "process_resident_memory_bytes",
    "process_cpu_seconds_total",
}
DATASOURCE = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
SELECTOR = re.compile(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\{((?:"(?:[^"\\]|\\.)*"|[^"}])*)\}')


@pytest.fixture(scope="module")
def dashboard() -> JSON:
    """Load the same standalone document used by file provisioning.

    Returns
    -------
    JSON
        The checked-in dashboard, without an HTTP API wrapper.

    """
    return json.loads((Path(__file__).parents[1] / "dash.json").read_text(encoding="utf-8"))


def test_standalone_dashboard_has_stable_identity_and_builtin_panels(dashboard: JSON) -> None:
    """Import and provisioning must not need a legacy wrapper or panel plugin."""
    assert "dashboard" not in dashboard
    assert dashboard["id"] is None
    assert dashboard["uid"] == "tp-link-power"
    assert dashboard["schemaVersion"] >= 39
    assert "__inputs" not in dashboard
    assert {panel["type"] for panel in dashboard["panels"]} <= {
        "stat",
        "timeseries",
        "bargauge",
        "table",
        "text",
        "row",
    }
    assert dashboard["time"]["to"] == "now"


def test_panels_have_unique_ids_and_nonoverlapping_grid_positions(dashboard: JSON) -> None:
    """A malformed export must not hide panels behind each other or outside the grid."""
    panels = dashboard["panels"]
    assert len({panel["id"] for panel in panels}) == len(panels)
    for panel in panels:
        position = panel["gridPos"]
        assert 0 <= position["x"] < 24
        assert position["x"] + position["w"] <= 24
        assert position["y"] >= 0
        assert position["h"] > 0
        assert panel["title"]
    for first, second in combinations(panels, 2):
        left, right = first["gridPos"], second["gridPos"]
        overlaps_horizontally = left["x"] < right["x"] + right["w"] and right["x"] < left["x"] + left["w"]
        overlaps_vertically = left["y"] < right["y"] + right["h"] and right["y"] < left["y"] + left["h"]
        assert not (overlaps_horizontally and overlaps_vertically), (first["title"], second["title"])


def test_all_queries_use_the_selected_prometheus_datasource(dashboard: JSON) -> None:
    """Moving the dashboard must not retain source-instance IDs or API import inputs."""
    variables = {variable["name"]: variable for variable in dashboard["templating"]["list"]}
    source = variables["DS_PROMETHEUS"]
    assert source["type"] == "datasource"
    assert source["query"] == "prometheus"
    assert source["current"]["value"] == "prometheus"
    assert source["hide"] == 0
    for variable in variables.values():
        if variable["type"] == "query":
            assert variable["datasource"] == DATASOURCE
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            assert panel["datasource"] == DATASOURCE
            assert target["datasource"] == DATASOURCE
            assert target["expr"].strip()


def test_device_filters_support_regex_safe_multiple_and_all_selections(dashboard: JSON) -> None:
    """Dots in hosts and special characters in aliases must not broaden selections."""
    variables = {variable["name"]: variable for variable in dashboard["templating"]["list"]}
    for name in ("job", "exporter", "host", "device"):
        variable = variables[name]
        assert variable["multi"] is True
        assert variable["includeAll"] is True
        assert variable["allValue"] == ".*"
        assert variable["options"] == []
    assert "${host:regex}" in variables["device"]["query"]["query"]
    assert variables["host"]["current"]["value"] == "$__all"
    assert variables["device"]["current"]["value"] == "$__all"


def test_query_selectors_match_exporter_metrics_and_device_labels(dashboard: JSON) -> None:
    """Reject obsolete metrics, unscoped jobs and queries that lose device identity."""
    queried_device_metrics = set()
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expression = target["expr"]
            for metric, labels in SELECTOR.findall(expression):
                assert metric in DEVICE_UNITS or metric in INFRASTRUCTURE_METRICS, metric
                assert 'job=~"${job:regex}"' in labels
                assert 'instance=~"${exporter:regex}"' in labels
                if metric in DEVICE_UNITS:
                    queried_device_metrics.add(metric)
                    assert 'host=~"${host:regex}"' in labels
                    assert 'alias=~"${device:regex}"' in labels
                    assert "and on (job, instance)" in expression
                    assert " == 1" in expression
                    if panel["type"] in {"timeseries", "bargauge"} and not expression.startswith("sum("):
                        assert "{{host}}" in target["legendFormat"]
                        assert "{{instance}}" in target["legendFormat"]
    assert queried_device_metrics == DEVICE_UNITS.keys()


def test_native_measurements_keep_their_units_and_energy_gauges_are_not_counters(dashboard: JSON) -> None:
    """Wh values must not become kWh accidentally or depend on the scrape interval."""
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expression = target["expr"]
            for metric, _labels in SELECTOR.findall(expression):
                if metric not in DEVICE_UNITS:
                    continue
                if expression.startswith("count("):
                    continue
                assert panel["fieldConfig"]["defaults"]["unit"] == DEVICE_UNITS[metric]
                assert not re.search(r"\b(?:rate|irate|increase|sum_over_time)\(", expression)
                assert "/240" not in expression.replace(" ", "")
                if metric in {"current_consumption_today", "current_month_consumption"}:
                    assert "/" not in expression


def test_missing_samples_are_never_silently_converted_to_zero_or_connected(dashboard: JSON) -> None:
    """Offline or unsupported readings must remain visibly different from a real zero."""
    for panel in dashboard["panels"]:
        if "targets" not in panel:
            continue
        assert panel["fieldConfig"]["defaults"]["noValue"] in {"No data", "No targets"}
        assert panel["description"]
        for target in panel["targets"]:
            assert "or vector(0)" not in target["expr"]
            if panel["type"] in {"stat", "bargauge"}:
                assert target["instant"] is True
                assert target["range"] is False
        if panel["type"] == "timeseries":
            assert panel["fieldConfig"]["defaults"]["custom"]["spanNulls"] is False


def test_health_panels_do_not_invent_device_freshness_or_online_state(dashboard: JSON) -> None:
    """Scrape timestamps and inventory cannot establish a device reading's freshness."""
    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    assert "not the age of the device reading" in panels["Scrape age"]["description"]
    assert "not an online count" in panels["Reporting plugs"]["description"]
    assert "not an online count" in panels["Inventory"]["description"]
    guidance = panels["When data is missing"]["options"]["content"]
    assert "older snapshot" in guidance
    assert "exporter logs" in guidance
    assert "not counters" in guidance
