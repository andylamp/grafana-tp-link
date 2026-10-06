# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exercise storage selection without touching host data or starting containers."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from grafana_tp_link import configuration

ROOT = Path(__file__).parents[1]
Settings = dict[str, Any]


@pytest.fixture
def storage_settings() -> Settings:
    """Provide minimal valid settings with no implicit host storage paths.

    Returns
    -------
    Settings
        Isolated stack settings containing only a documentation device address.
    """
    return {
        "grafana": {"bind_address": "127.0.0.1", "port": 3000},
        "prometheus": {
            "retention_time": "3y",
            "retention_size": "10GB",
            "scrape_interval": "30s",
            "scrape_timeout": "25s",
            "evaluation_interval": "30s",
        },
        "exporter": {"exporters": {"tapo": {"devices": ["192.0.2.10"]}}},
    }


@pytest.fixture
def storage_checkout(tmp_path: Path, storage_settings: Settings) -> Path:
    """Create a checkout with the templates needed for offline rendering.

    Returns
    -------
    Path
        Checkout directory containing no credentials or persistent application data.
    """
    directory = tmp_path / "monitoring checkout"
    directory.mkdir()
    (directory / "config").mkdir()
    _write_settings(directory, storage_settings)
    for relative in ("prometheus/prometheus.yml", "grafana/provisioning/datasources/prometheus.yaml"):
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return directory


def _write_settings(directory: Path, settings: Settings) -> None:
    """Save comments and test settings without adding credentials."""
    (directory / "config" / "stack.yaml").write_text(
        "# Preserve storage and device notes.\n" + yaml.safe_dump(settings), encoding="utf-8"
    )


def _render_storage(directory: Path, settings: Settings) -> Settings:
    """Read the generated storage model for inspection.

    Returns
    -------
    Settings
        Parsed Compose override using explicit long-form mount syntax.
    """
    runtime = configuration.render_configuration(directory, settings)
    return yaml.safe_load((runtime / "compose.storage.yaml").read_text(encoding="utf-8"))


def test_omitted_and_null_storage_reuse_existing_managed_volumes(
    storage_checkout: Path, storage_settings: Settings
) -> None:
    """The default preserves both existing volume names and permits optional null settings."""
    effective = configuration.load_configuration(storage_checkout, {})
    for service in ("grafana", "prometheus"):
        options = effective[service]
        assert isinstance(options, dict)
        assert "data_directory" not in options
    first = configuration.render_configuration(storage_checkout, effective)
    document = yaml.safe_load((first / "compose.storage.yaml").read_text(encoding="utf-8"))
    assert document == {
        "services": {
            "grafana": {"volumes": [{"type": "volume", "source": "grafana-data", "target": "/var/lib/grafana"}]},
            "prometheus": {"volumes": [{"type": "volume", "source": "prometheus-data", "target": "/prometheus"}]},
        }
    }
    for name in ("grafana", "prometheus"):
        storage_settings[name]["data_directory"] = None
    _write_settings(storage_checkout, storage_settings)
    second = configuration.render_configuration(
        storage_checkout, configuration.load_configuration(storage_checkout, {})
    )
    assert second == first


@pytest.mark.parametrize("service", ["grafana", "prometheus"])
def test_each_service_can_select_a_bind_independently(
    storage_checkout: Path, storage_settings: Settings, service: str
) -> None:
    """Selecting one host directory leaves the other service's managed volume intact."""
    storage_settings[service]["data_directory"] = f"data/{service}"
    _write_settings(storage_checkout, storage_settings)
    effective = configuration.load_configuration(storage_checkout, {})
    document = _render_storage(storage_checkout, effective)
    target = "/var/lib/grafana" if service == "grafana" else "/prometheus"
    assert document["services"][service]["volumes"] == [
        {
            "type": "bind",
            "source": str(storage_checkout / "data" / service),
            "target": target,
            "bind": {"create_host_path": False},
        }
    ]
    other = "prometheus" if service == "grafana" else "grafana"
    assert document["services"][other]["volumes"][0]["type"] == "volume"
    assert not (storage_checkout / "data").exists()


@pytest.mark.parametrize("service", ["grafana", "prometheus"])
def test_storage_override_replaces_yaml_while_empty_uses_yaml(
    storage_checkout: Path, storage_settings: Settings, service: str
) -> None:
    """Resolved nonempty environment paths win; empty values retain the selected YAML path."""
    storage_settings[service]["data_directory"] = "original data"
    _write_settings(storage_checkout, storage_settings)
    name = f"{service.upper()}_DATA_DIRECTORY"
    overridden = configuration.load_configuration(storage_checkout, {name: "replacement data"})
    overridden_options = overridden[service]
    assert isinstance(overridden_options, dict)
    assert overridden_options["data_directory"] == "replacement data"
    assert _render_storage(storage_checkout, overridden)["services"][service]["volumes"][0]["source"] == str(
        storage_checkout / "replacement data"
    )
    original = configuration.load_configuration(storage_checkout, {name: ""})
    original_options = original[service]
    assert isinstance(original_options, dict)
    assert original_options["data_directory"] == "original data"
    assert not (storage_checkout / "replacement data").exists()
    assert not (storage_checkout / "original data").exists()


@pytest.mark.parametrize("service", ["grafana", "prometheus"])
@pytest.mark.parametrize("value", ["", " \t\n", False, 123, [], {}, "private\0path"])
def test_invalid_storage_values_fail_before_runtime_publication(
    storage_checkout: Path, storage_settings: Settings, service: str, value: object
) -> None:
    """Invalid path types and text cannot reach Docker or leak their values in errors."""
    storage_settings[service]["data_directory"] = value
    _write_settings(storage_checkout, storage_settings)
    with pytest.raises(ValueError, match="data_directory must be null or a nonempty path") as error:
        configuration.load_configuration(storage_checkout, {})
    assert "private" not in str(error.value)
    assert not (storage_checkout / ".runtime").exists()


@pytest.mark.parametrize("value", [" \t", "private\0path"])
def test_invalid_nonempty_storage_overrides_do_not_fall_back(storage_checkout: Path, value: str) -> None:
    """Malformed explicit overrides fail instead of choosing unrelated stored data."""
    with pytest.raises(ValueError, match="data_directory"):
        configuration.load_configuration(storage_checkout, {"GRAFANA_DATA_DIRECTORY": value})
    assert not (storage_checkout / ".runtime").exists()


def test_selected_yaml_resolves_storage_from_checkout_and_preserves_literal_dollars(
    storage_checkout: Path, storage_settings: Settings, tmp_path: Path
) -> None:
    """Moving the selected settings file cannot redirect relative storage or interpolate path names."""
    selected = tmp_path / "external settings.yaml"
    storage_settings["grafana"]["data_directory"] = "data with spaces/$HOME/${GRAFANA_PORT}/$$grafana"
    absolute = tmp_path / "absolute data" / "prometheus"
    storage_settings["prometheus"]["data_directory"] = str(absolute)
    selected.write_text("# Keep these comments.\n" + yaml.safe_dump(storage_settings), encoding="utf-8")
    before = selected.read_bytes()
    (storage_checkout / "config" / "stack.yaml").write_text("invalid: [", encoding="utf-8")
    effective = configuration.load_configuration(storage_checkout, {}, config_path=selected)
    document = _render_storage(storage_checkout, effective)
    assert document["services"]["grafana"]["volumes"][0]["source"] == str(
        storage_checkout / "data with spaces" / "$$HOME" / "$${GRAFANA_PORT}" / "$$$$grafana"
    )
    assert document["services"]["prometheus"]["volumes"][0]["source"] == str(absolute)
    assert selected.read_bytes() == before
    assert not (storage_checkout / "data with spaces").exists()
    assert not absolute.exists()


def test_storage_paths_do_not_follow_client_side_symlinks(
    storage_checkout: Path, storage_settings: Settings, tmp_path: Path
) -> None:
    """A local symlink cannot redirect a bind intended for the Docker daemon's filesystem."""
    local_target = tmp_path / "client-only directory"
    local_target.mkdir()
    alias = storage_checkout / "daemon data"
    alias.symlink_to(local_target, target_is_directory=True)
    storage_settings["grafana"]["data_directory"] = alias.name
    _write_settings(storage_checkout, storage_settings)
    effective = configuration.load_configuration(storage_checkout, {})
    document = _render_storage(storage_checkout, effective)
    assert document["services"]["grafana"]["volumes"][0]["source"] == str(alias)
    assert alias.is_symlink()
    assert not list(local_target.iterdir())


def test_storage_change_changes_generation_without_rewriting_native_files_or_existing_data(
    storage_checkout: Path, storage_settings: Settings, tmp_path: Path
) -> None:
    """Storage selection updates Compose identity while leaving native config and owned data intact."""
    baseline = configuration.load_configuration(storage_checkout, {})
    initial = configuration.render_configuration(storage_checkout, baseline)
    initial_files = {path.name: path.read_bytes() for path in initial.iterdir()}
    data = tmp_path / "existing data"
    data.mkdir(mode=0o750)
    sentinel = data / "database"
    sentinel.write_bytes(b"existing application data")
    before = data.stat(), sentinel.stat().st_mtime_ns
    storage_settings["grafana"]["data_directory"] = str(data)
    _write_settings(storage_checkout, storage_settings)
    source = (storage_checkout / "config" / "stack.yaml").read_bytes()
    effective = configuration.load_configuration(storage_checkout, {})
    original = copy.deepcopy(effective)
    updated = configuration.render_configuration(storage_checkout, effective)
    assert updated != initial
    assert effective == original
    assert (storage_checkout / "config" / "stack.yaml").read_bytes() == source
    assert (data.stat(), sentinel.stat().st_mtime_ns) == before
    assert sentinel.read_bytes() == b"existing application data"
    assert {path.name: path.read_bytes() for path in initial.iterdir()} == initial_files
    for name in ("exporter.yaml", "prometheus.yml", "datasource.yaml"):
        assert (updated / name).read_bytes() == initial_files[name]
    assert (updated / "compose.storage.yaml").read_bytes() != initial_files["compose.storage.yaml"]


@pytest.mark.parametrize("address", ["localhost", "LOCALHOST", "LocalHost."])
def test_localhost_normalizes_to_loopback_without_modifying_source(
    storage_checkout: Path, storage_settings: Settings, address: str
) -> None:
    """The explicit localhost spelling is portable without DNS resolution or source rewrites."""
    storage_settings["grafana"]["bind_address"] = address
    _write_settings(storage_checkout, storage_settings)
    source = storage_checkout / "config" / "stack.yaml"
    before = source.read_bytes()
    effective = configuration.load_configuration(storage_checkout, {})
    runtime = configuration.render_configuration(storage_checkout, effective)
    grafana = effective["grafana"]
    assert isinstance(grafana, dict)
    assert grafana["bind_address"] == "127.0.0.1"
    assert configuration.compose_environment(effective, runtime)["GRAFANA_BIND_ADDRESS"] == "127.0.0.1"
    assert source.read_bytes() == before
