# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Validate the monitoring contract without starting Docker or contacting devices."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tests.integration.support import mapping
from tests.integration.test_stack_smoke import expand, expressions

ROOT = Path(__file__).resolve().parents[1]


def test_scraping_uses_single_exporter_and_a_compatible_deadline() -> None:
    """Prometheus should scrape the exporter once with room for HTTP transport."""
    prometheus = mapping(yaml.safe_load((ROOT / "prometheus/prometheus.yml").read_text()))
    jobs = prometheus["scrape_configs"]
    assert isinstance(jobs, list)
    job = next(mapping(value) for value in jobs if mapping(value)["job_name"] == "pyprom-exporters")
    assert job["static_configs"] == [{"targets": ["exporter:8090"]}]
    assert job["metrics_path"] == "/metrics"
    assert "relabel_configs" not in job
    config = mapping(yaml.safe_load((ROOT / "config/exporter.yaml").read_text()))
    exporter = mapping(mapping(config["exporters"])["tapo"])
    assert mapping(exporter["discovery_options"])["perform_discovery"] is False
    assert exporter["devices"] == []
    timeout = mapping(prometheus["global"])["scrape_timeout"]
    interval = mapping(prometheus["global"])["scrape_interval"]
    assert float(str(mapping(exporter["prometheus_options"])["scrape_timeout"])) < float(
        str(timeout).removesuffix("s")
    )
    assert float(str(timeout).removesuffix("s")) <= float(str(interval).removesuffix("s"))


def test_compose_scopes_storage_and_protects_local_configuration() -> None:
    """Safe defaults avoid global container names and writable config mounts."""
    model = mapping(yaml.safe_load((ROOT / "compose.yaml").read_text()))
    # Reusing the old launcher identity could replace or stop its live containers.
    assert model["name"] != "grafana-tp-link"
    services = mapping(model["services"])
    assert set(services) == {"exporter", "prometheus", "grafana"}
    for service in services.values():
        definition = mapping(service)
        assert "container_name" not in definition
        assert definition.get("network_mode") != "host"
        assert "@sha256:" in str(definition["image"])
    exporter = mapping(services["exporter"])
    command = exporter["command"]
    assert isinstance(command, list)
    assert "--no-write-config" in command
    assert not exporter.get("ports")
    assert not mapping(services["prometheus"]).get("ports")
    assert "127.0.0.1" in str(mapping(services["grafana"])["ports"])
    for volume in mapping(model["volumes"]).values():
        assert volume is None or not mapping(volume).get("external")
    assert "depends_on" not in mapping(services["prometheus"])


def test_dashboard_uses_native_units_host_identity_and_provisioned_datasource() -> None:
    """Dashboard expressions retain host identity and avoid rate on energy gauges."""
    dashboard = mapping(json.loads((ROOT / "dash.json").read_text()))
    source = mapping(yaml.safe_load((ROOT / "grafana/provisioning/datasources/prometheus.yaml").read_text()))
    datasources = source["datasources"]
    assert isinstance(datasources, list)
    assert mapping(datasources[0])["uid"] == "prometheus"
    assert dashboard["uid"] == "tp-link-power"
    queries = list(expressions(dashboard))
    assert queries
    for query in queries:
        expanded = expand(query)
        assert "tplink_" not in expanded
        if "current_" in expanded:
            assert "host=~" in expanded
            assert "alias=~" in expanded
        for energy_metric in ("current_consumption_today", "current_month_consumption"):
            assert f"rate({energy_metric}" not in expanded
            assert f"increase({energy_metric}" not in expanded
