# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Validate the monitoring contract without starting Docker or contacting devices."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from grafana_tp_link.configuration import load_configuration, render_configuration
from tests.integration import support
from tests.integration.support import TEST_PASSWORD, isolated_exporter_configuration, mapping, prepare_checkout
from tests.integration.test_stack_smoke import expand, expressions

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_scraping_uses_single_exporter_and_a_compatible_deadline(tmp_path: Path) -> None:
    """Prometheus should scrape the exporter once with room for HTTP transport."""
    prepare_checkout(tmp_path)
    configuration = load_configuration(tmp_path, {}, require_devices=False)
    runtime = render_configuration(tmp_path, configuration)
    prometheus = mapping(yaml.safe_load((runtime / "prometheus.yml").read_text()))
    config = mapping(yaml.safe_load((runtime / "exporter.yaml").read_text()))
    jobs = prometheus["scrape_configs"]
    assert isinstance(jobs, list)
    job = next(mapping(value) for value in jobs if mapping(value)["job_name"] == "pyprom-exporters")
    assert job["static_configs"] == [{"targets": [f"exporter:{config['prometheus_port']}"]}]
    assert job["metrics_path"] == "/metrics"
    assert "relabel_configs" not in job
    exporter = mapping(mapping(config["exporters"])["tapo"])
    assert mapping(exporter["discovery_options"])["perform_discovery"] is False
    assert isinstance(exporter["devices"], list)
    timeout = mapping(prometheus["global"])["scrape_timeout"]
    interval = mapping(prometheus["global"])["scrape_interval"]
    assert timeout == mapping(configuration["prometheus"])["scrape_timeout"]
    assert interval == mapping(configuration["prometheus"])["scrape_interval"]
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


def test_integration_configuration_removes_user_device_targets_and_credentials() -> None:
    """User-edited device lists cannot make Docker tests probe real devices."""
    original = {
        "prometheus_port": 8090,
        "exporters": {
            "tapo": {
                "devices": ["192.0.2.10"],
                "max_concurrent_devices": 7,
                "discovery_options": {
                    "perform_discovery": True,
                    "timeout": 4,
                    "credentials": {"username": "fixture-user", "password": TEST_PASSWORD},
                },
            }
        },
    }
    isolated = mapping(yaml.safe_load(isolated_exporter_configuration(yaml.safe_dump(original))))
    exporter = mapping(mapping(isolated["exporters"])["tapo"])
    assert exporter["devices"] == []
    assert exporter["max_concurrent_devices"] == 7
    assert exporter["discovery_options"] == {"perform_discovery": False, "timeout": 4}
    assert isolated["prometheus_port"] == 8090


def test_integration_checkout_cannot_mount_user_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user-edited data path is replaced by test-project volumes before Docker sees it."""
    source = tmp_path / "source"
    config = prepare_checkout(source)
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    marker = user_data / "keep.txt"
    marker.write_text("Existing data must remain untouched.")
    for service in ("grafana", "prometheus"):
        mapping(config[service])["data_directory"] = str(user_data)
    (source / "config/stack.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setattr(support, "ROOT", source)
    directory = tmp_path / "isolated"
    prepare_checkout(directory)
    effective = load_configuration(directory, {}, require_devices=False)
    runtime = render_configuration(directory, effective)
    storage = mapping(yaml.safe_load((runtime / "compose.storage.yaml").read_text()))
    for service in ("grafana", "prometheus"):
        volumes = mapping(mapping(storage["services"])[service])["volumes"]
        assert isinstance(volumes, list)
        assert mapping(volumes[0])["type"] == "volume"
        assert mapping(volumes[0])["source"] == f"{service}-data"
    assert marker.read_text() == "Existing data must remain untouched."
