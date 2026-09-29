# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exercise unified settings, cross-service consistency and atomic publication."""

from __future__ import annotations

import copy
import json
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml

from grafana_tp_link import configuration

Settings = dict[str, Any]
ROOT = Path(__file__).parents[1]


@pytest.fixture
def settings() -> Settings:
    """Provide a native exporter configuration and all shared stack settings.

    Returns
    -------
    Settings
        Fresh settings containing two documentation-only hosts.
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
        "exporter": {
            "prometheus_port": 8090,
            "log_level": "INFO",
            "write_non_default_config": False,
            "exporters": {
                "tapo": {
                    "devices": ["192.0.2.10", "kitchen-plug.example"],
                    "max_concurrent_devices": 7,
                    "discovery_options": {"perform_discovery": False, "tapo_username_env_key": "TP_LINK_USERNAME"},
                    "prometheus_options": {"refresh_interval": None, "scrape_timeout": 20.0},
                    "per_device_family_metrics": {"plug": {}},
                }
            },
        },
    }


def write_settings(directory: Path, settings: Settings) -> Path:
    """Write a commented source document for validation scenarios.

    Returns
    -------
    Path
        Path to the source file.
    """
    path = directory / "config" / "stack.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text("# Preserve these device notes.\n" + yaml.safe_dump(settings), encoding="utf-8")
    return path


@pytest.fixture
def checkout(tmp_path: Path, settings: Settings) -> Path:
    """Copy only the source templates needed for offline configuration rendering.

    Returns
    -------
    Path
        Isolated checkout containing no credentials or live device addresses.
    """
    directory = tmp_path / "monitoring checkout"
    directory.mkdir()
    write_settings(directory, settings)
    for relative in ("prometheus/prometheus.yml", "grafana/provisioning/datasources/prometheus.yaml"):
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return directory


def test_loading_preserves_source_comments_native_options_and_ignores_credentials(checkout: Path) -> None:
    """Credentials stay outside the returned settings, and native metric configuration survives."""
    path = checkout / "config" / "stack.yaml"
    before = path.read_bytes()
    result = configuration.load_configuration(checkout, {"TP_LINK_PASSWORD": "PRIVATE_TEST_VALUE"})
    native = result["exporter"]
    assert isinstance(native, dict)
    assert native["exporters"]["tapo"]["max_concurrent_devices"] == 7
    assert native["exporters"]["tapo"]["per_device_family_metrics"] == {"plug": {}}
    assert native["exporters"]["tapo"]["discovery_options"]["tapo_username_env_key"] == "TP_LINK_USERNAME"
    assert "PRIVATE_TEST_VALUE" not in json.dumps(result)
    assert path.read_bytes() == before
    assert not (checkout / ".runtime").exists()


def test_every_supported_override_updates_its_effective_setting(checkout: Path) -> None:
    """Environment overrides replace YAML consistently across all supported settings."""
    grafana_data = checkout / "grafana data"
    prometheus_data = checkout / "prometheus data"
    grafana_data.mkdir()
    prometheus_data.mkdir()
    overrides = {
        "GRAFANA_BIND_ADDRESS": "::1",
        "GRAFANA_PORT": "3333",
        "GRAFANA_DATA_DIRECTORY": str(grafana_data),
        "PROMETHEUS_DATA_DIRECTORY": str(prometheus_data),
        "PROMETHEUS_RETENTION_TIME": "90d",
        "PROMETHEUS_RETENTION_SIZE": "1.5GB",
        "PROMETHEUS_SCRAPE_INTERVAL": "1m",
        "PROMETHEUS_SCRAPE_TIMEOUT": "35s",
        "PROMETHEUS_EVALUATION_INTERVAL": "15s",
        "PROMETHEUS_PORT": "18090",
        "PYPROM_EXPORTERS_LOG_LEVEL": "debug",
        "TAPO_PLUG_DEVICES": "192.0.2.20, garage-plug.example",
    }
    assert set(overrides) == set(configuration.OVERRIDE_NAMES)
    effective = configuration.load_configuration(checkout, overrides)
    assert effective["grafana"] == {"bind_address": "::1", "port": 3333, "data_directory": str(grafana_data)}
    assert effective["prometheus"] == {
        "data_directory": str(prometheus_data),
        "retention_time": "90d",
        "retention_size": "1.5GB",
        "scrape_interval": "1m",
        "scrape_timeout": "35s",
        "evaluation_interval": "15s",
    }
    exporter = effective["exporter"]
    assert isinstance(exporter, dict)
    assert exporter["prometheus_port"] == 18090
    assert exporter["log_level"] == "DEBUG"
    assert exporter["exporters"]["tapo"]["devices"] == ["192.0.2.20", "garage-plug.example"]
    expected = configuration.load_configuration(checkout, {})
    assert configuration.load_configuration(checkout, dict.fromkeys(configuration.OVERRIDE_NAMES, "")) == expected


@pytest.mark.parametrize("value", [" ", ",,,", "\t,\n"])
def test_nonempty_but_empty_host_override_never_falls_back(checkout: Path, value: str) -> None:
    """Whitespace/comma overrides cannot silently select the original device inventory."""
    with pytest.raises(ValueError, match="at least one host"):
        configuration.load_configuration(checkout, {"TAPO_PLUG_DEVICES": value}, require_devices=False)


def test_only_explicit_offline_loading_permits_empty_device_inventory(checkout: Path, settings: Settings) -> None:
    """Normal startup fails closed while offline rendering can avoid physical devices."""
    settings["exporter"]["exporters"]["tapo"]["devices"] = []
    write_settings(checkout, settings)
    with pytest.raises(ValueError, match="at least one"):
        configuration.load_configuration(checkout, {})
    assert configuration.load_configuration(checkout, {}, require_devices=False)["exporter"] == settings["exporter"]


@pytest.mark.parametrize("hosts", [[None], [123], [""], ["two hosts"], ["one,two"], ["https://plug.example"], "plug"])
def test_device_inventory_requires_individual_literal_hosts(checkout: Path, settings: Settings, hosts: object) -> None:
    """Lists cannot contain malformed values or treat a single string as an inventory."""
    settings["exporter"]["exporters"]["tapo"]["devices"] = hosts
    write_settings(checkout, settings)
    with pytest.raises(ValueError, match="individual IPs or hostnames"):
        configuration.load_configuration(checkout, {})


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("grafana", "port", True),
        ("grafana", "port", 0),
        ("grafana", "port", 65536),
        ("grafana", "port", "3000"),
        ("grafana", "bind_address", "hostname.example"),
        ("exporter", "prometheus_port", -1),
        ("exporter", "log_level", "verbose"),
        ("prometheus", "scrape_interval", "0s"),
        ("prometheus", "scrape_interval", "1m1h"),
        ("prometheus", "scrape_interval", "1.5s"),
        ("prometheus", "retention_time", "1000y"),
        ("prometheus", "retention_size", "-1GB"),
        ("prometheus", "retention_size", "10GiB"),
        ("prometheus", "evaluation_interval", 30),
    ],
)
def test_invalid_scalars_fail_without_echoing_values(
    checkout: Path, settings: Settings, section: str, key: str, value: object
) -> None:
    """Reject invalid ports, addresses, logging levels and Prometheus units before rendering."""
    settings[section][key] = value
    write_settings(checkout, settings)
    with pytest.raises(ValueError, match="must"):
        configuration.load_configuration(checkout, {})
    assert not (checkout / ".runtime").exists()


@pytest.mark.parametrize(("timeout", "interval"), [("20s", "30s"), ("31s", "30s")])
def test_refresh_wait_and_scrape_budgets_cannot_conflict(checkout: Path, timeout: str, interval: str) -> None:
    """Exporter refresh waits must fit within Prometheus's scrape interval and timeout."""
    with pytest.raises(ValueError, match="scrape_timeout < Prometheus scrape_timeout <= scrape_interval"):
        configuration.load_configuration(
            checkout, {"PROMETHEUS_SCRAPE_TIMEOUT": timeout, "PROMETHEUS_SCRAPE_INTERVAL": interval}
        )
    assert configuration.load_configuration(
        checkout, {"PROMETHEUS_SCRAPE_TIMEOUT": "30s", "PROMETHEUS_SCRAPE_INTERVAL": "30s"}
    )


@pytest.mark.parametrize("section", ["grafana", "prometheus"])
def test_unknown_stack_keys_cannot_be_silently_ignored(checkout: Path, settings: Settings, section: str) -> None:
    """Misspelled service settings must fail instead of appearing to take effect."""
    settings[section]["PRIVATE_TEST_KEY"] = "PRIVATE_TEST_VALUE"
    write_settings(checkout, settings)
    with pytest.raises(ValueError, match="must contain exactly") as error:
        configuration.load_configuration(checkout, {})
    assert "PRIVATE_TEST" not in str(error.value)


@pytest.mark.parametrize(
    "path",
    [
        ("grafana", "admin_user"),
        ("grafana", "admin_password"),
        ("exporter", "password"),
        ("exporter", "exporters", "tapo", "discovery_options", "credentials"),
    ],
)
def test_yaml_credentials_are_rejected_before_any_output(
    checkout: Path, settings: Settings, path: tuple[str, ...]
) -> None:
    """All credential paths stay in the environment, even if an override would hide them."""
    target = settings
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = "PRIVATE_TEST_VALUE"
    write_settings(checkout, settings)
    with pytest.raises(ValueError, match="Credentials") as error:
        configuration.load_configuration(checkout, {})
    assert "PRIVATE_TEST_VALUE" not in str(error.value)
    assert not (checkout / ".runtime").exists()


def test_yaml_parser_errors_and_recursive_aliases_do_not_leak_source(checkout: Path) -> None:
    """Malformed and recursive YAML is reported without printing source contents."""
    path = checkout / "config" / "stack.yaml"
    path.write_text("value: [PRIVATE_TEST_VALUE", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML syntax") as error:
        configuration.load_configuration(checkout, {})
    assert "PRIVATE_TEST_VALUE" not in str(error.value)
    path.write_text("loop: &loop [*loop]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Recursive"):
        configuration.load_configuration(checkout, {})


def test_rendering_synchronizes_ports_and_intervals_without_modifying_sources(checkout: Path) -> None:
    """All consumers see the same effective settings, with container-readable immutable files."""
    before = {path: path.read_bytes() for path in checkout.rglob("*.y*ml")}
    effective = configuration.load_configuration(
        checkout, {"PROMETHEUS_PORT": "18090", "PROMETHEUS_SCRAPE_INTERVAL": "1m"}
    )
    original = copy.deepcopy(effective)
    runtime = configuration.render_configuration(checkout, effective)
    exporter = yaml.safe_load((runtime / "exporter.yaml").read_text())
    prometheus = yaml.safe_load((runtime / "prometheus.yml").read_text())
    datasource = yaml.safe_load((runtime / "datasource.yaml").read_text())
    assert exporter == effective["exporter"]
    assert prometheus["global"] == {"scrape_interval": "1m", "scrape_timeout": "25s", "evaluation_interval": "30s"}
    assert prometheus["scrape_configs"][0]["static_configs"] == [{"targets": ["exporter:18090"]}]
    assert datasource["datasources"][0]["jsonData"]["timeInterval"] == "1m"
    assert datasource["datasources"][0]["uid"] == "prometheus"
    assert {path.name for path in runtime.iterdir()} == {
        "exporter.yaml",
        "prometheus.yml",
        "datasource.yaml",
        "compose.storage.yaml",
    }
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in runtime.iterdir())
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o755
    assert {path: path.read_bytes() for path in before} == before
    assert effective == original
    assert runtime.parent == checkout / ".runtime"
    assert configuration.render_configuration(checkout, effective) == runtime
    replacement = configuration.load_configuration(checkout, {"PROMETHEUS_PORT": "18091"})
    assert configuration.render_configuration(checkout, replacement) != runtime
    assert yaml.safe_load((runtime / "exporter.yaml").read_text()) == exporter


def test_short_intervals_do_not_retain_an_incompatible_self_scrape_timeout(checkout: Path, settings: Settings) -> None:
    """The template's old 10s self-scrape timeout cannot invalidate a 3s global interval."""
    settings["exporter"]["exporters"]["tapo"]["prometheus_options"]["scrape_timeout"] = 1.0
    write_settings(checkout, settings)
    effective = configuration.load_configuration(
        checkout, {"PROMETHEUS_SCRAPE_INTERVAL": "3s", "PROMETHEUS_SCRAPE_TIMEOUT": "2s"}
    )
    runtime = configuration.render_configuration(checkout, effective)
    prometheus = yaml.safe_load((runtime / "prometheus.yml").read_text())
    job = next(job for job in prometheus["scrape_configs"] if job["job_name"] == "prometheus")
    assert "scrape_timeout" not in job
    assert prometheus["global"]["scrape_timeout"] == "2s"


def test_compose_environment_keeps_effective_values_and_no_credentials(checkout: Path) -> None:
    """Compose uses effective YAML, IPv6-safe bindings and the exact rendered generation."""
    effective = configuration.load_configuration(checkout, {"GRAFANA_BIND_ADDRESS": "::1", "GRAFANA_PORT": "3333"})
    runtime = configuration.render_configuration(checkout, effective)
    environment = configuration.compose_environment(effective, runtime)
    assert environment == {
        "GRAFANA_BIND_ADDRESS": "[::1]",
        "GRAFANA_PORT": "3333",
        "PROMETHEUS_RETENTION_TIME": "3y",
        "PROMETHEUS_RETENTION_SIZE": "10GB",
        "PROMETHEUS_PORT": "8090",
        "PYPROM_EXPORTERS_LOG_LEVEL": "INFO",
        "TAPO_PLUG_DEVICES": "192.0.2.10 kitchen-plug.example",
        "PYPROM_RUNTIME_DIR": str(runtime.resolve()),
    }


def test_concurrent_renderers_publish_one_complete_generation(checkout: Path) -> None:
    """Concurrent commands cannot overwrite a generation or leave partial output visible."""
    effective = configuration.load_configuration(checkout, {})
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(configuration.render_configuration, checkout, effective) for _ in range(8)]
        directories = {future.result() for future in futures}
    assert len(directories) == 1
    assert list((checkout / ".runtime").iterdir()) == list(directories)
    assert len(list(next(iter(directories)).iterdir())) == 4


def test_failed_render_never_publishes_a_partial_generation(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disk failures remove staging files without creating a usable-looking generation."""

    def fail_sync(_descriptor: int) -> None:
        message = "simulated full disk"
        raise OSError(message)

    monkeypatch.setattr(configuration.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="simulated full disk"):
        configuration.render_configuration(checkout, configuration.load_configuration(checkout, {}))
    assert list((checkout / ".runtime").iterdir()) == []


def test_changed_generation_is_not_overwritten(checkout: Path) -> None:
    """Tampering cannot silently replace files already mounted by running containers."""
    effective = configuration.load_configuration(checkout, {})
    runtime = configuration.render_configuration(checkout, effective)
    target = runtime / "exporter.yaml"
    target.write_text("changed by somebody else\n", encoding="utf-8")
    with pytest.raises(ValueError, match="will not be overwritten"):
        configuration.render_configuration(checkout, effective)
    assert target.read_text() == "changed by somebody else\n"


def test_runtime_symlink_cannot_redirect_generated_writes(checkout: Path, tmp_path: Path) -> None:
    """Rendering cannot follow a runtime symlink to an unrelated directory."""
    other = tmp_path / "other"
    other.mkdir()
    (checkout / ".runtime").symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        configuration.render_configuration(checkout, configuration.load_configuration(checkout, {}))
    assert list(other.iterdir()) == []


@pytest.mark.parametrize(
    "content",
    [
        "grafana: {}\ngrafana: {}\n",
        "exporter:\n  exporters:\n    tapo:\n      devices: []\n      devices: []\n",
    ],
)
def test_duplicate_yaml_keys_cannot_silently_replace_settings(checkout: Path, content: str) -> None:
    """Repeated service blocks and inventories fail instead of hiding earlier values."""
    (checkout / "config" / "stack.yaml").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="YAML syntax"):
        configuration.load_configuration(checkout, {})


def test_yaml_merge_defaults_remain_supported_without_mutating_the_source(checkout: Path) -> None:
    """Duplicate-key protection retains intentional YAML merge-key override semantics."""
    path = checkout / "config" / "stack.yaml"
    text = path.read_text().replace(
        "grafana:\n  bind_address: 127.0.0.1\n  port: 3000",
        "grafana:\n  <<: {bind_address: 127.0.0.1, port: 3000}\n  port: 3333",
    )
    path.write_text(text, encoding="utf-8")
    result = configuration.load_configuration(checkout, {})
    assert result["grafana"] == {"bind_address": "127.0.0.1", "port": 3333}
    assert path.read_text() == text


def test_rendered_exporter_target_retains_static_labels(checkout: Path) -> None:
    """Changing the exporter port does not discard additional scrape labels."""
    path = checkout / "prometheus" / "prometheus.yml"
    template = yaml.safe_load(path.read_text())
    template["scrape_configs"][0]["static_configs"][0]["labels"] = {"site": "home"}
    path.write_text(yaml.safe_dump(template), encoding="utf-8")
    result = configuration.render_configuration(
        checkout, configuration.load_configuration(checkout, {"PROMETHEUS_PORT": "18090"})
    )
    rendered = yaml.safe_load((result / "prometheus.yml").read_text())
    assert rendered["scrape_configs"][0]["static_configs"] == [
        {"targets": ["exporter:18090"], "labels": {"site": "home"}}
    ]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("max_concurrent_devices",), 0),
        (("max_concurrent_devices",), -1),
        (("max_concurrent_devices",), None),
        (("max_concurrent_devices",), True),
        (("max_concurrent_devices",), 1.5),
        (("prometheus_options", "refresh_interval"), 0),
        (("prometheus_options", "refresh_interval"), -1),
        (("prometheus_options", "refresh_interval"), True),
        (("prometheus_options", "refresh_interval"), 0.5),
        (("prometheus_options", "refresh_interval"), "5"),
        (("discovery_options", "discovery_timeout"), 0),
        (("discovery_options", "discovery_timeout"), -5),
        (("discovery_options", "discovery_timeout"), None),
        (("discovery_options", "discovery_timeout"), True),
        (("discovery_options", "timeout"), -5),
        (("discovery_options", "timeout"), True),
        (("discovery_options", "discovery_packets"), 0),
        (("discovery_options", "discovery_packets"), 1.5),
        (("discovery_options", "discovery_packets"), True),
        (("discovery_options", "port"), 0),
        (("discovery_options", "port"), 65536),
        (("discovery_options", "port"), True),
    ],
)
def test_invalid_advertised_native_options_fail_before_rendering(
    checkout: Path, settings: Settings, path: tuple[str, ...], value: object
) -> None:
    """Checks reject unusable concurrency, polling and discovery settings before Docker starts."""
    options = settings["exporter"]["exporters"]["tapo"]
    for key in path[:-1]:
        options = options[key]
    options[path[-1]] = value
    write_settings(checkout, settings)
    with pytest.raises(ValueError, match=path[-1]):
        configuration.load_configuration(checkout, {})
    assert not (checkout / ".runtime").exists()


@pytest.mark.parametrize("poll_interval", [None, 5])
def test_valid_native_options_and_optional_defaults_remain_unchanged(
    checkout: Path, settings: Settings, poll_interval: int | None
) -> None:
    """Live or cached operation retains configured discovery values and optional native defaults."""
    tapo = settings["exporter"]["exporters"]["tapo"]
    tapo["max_concurrent_devices"] = 1
    tapo["prometheus_options"]["refresh_interval"] = poll_interval
    tapo["discovery_options"] = {
        "discovery_timeout": 5,
        "discovery_packets": 1,
        "timeout": None,
        "port": None,
    }
    write_settings(checkout, settings)
    assert configuration.load_configuration(checkout, {})["exporter"] == settings["exporter"]
    tapo["discovery_options"] = None
    write_settings(checkout, settings)
    assert configuration.load_configuration(checkout, {})["exporter"] == settings["exporter"]


@pytest.mark.parametrize("path_kind", ["relative", "absolute", "external"])
def test_explicit_source_resolution_and_override_precedence(
    checkout: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch, path_kind: str
) -> None:
    """Alternate paths select one complete stack document independently of the working directory."""
    default = checkout / "config" / "stack.yaml"
    original = default.read_bytes()
    selected = (checkout.parent if path_kind == "external" else checkout) / "custom stack.yaml"
    settings["grafana"]["port"] = 4321
    settings["exporter"]["prometheus_port"] = 18090
    settings["exporter"]["exporters"]["tapo"]["devices"] = ["192.0.2.80"]
    content = "# Keep this alternate device inventory.\n" + yaml.safe_dump(settings)
    selected.write_text(content, encoding="utf-8")
    config_path = selected.relative_to(checkout) if path_kind == "relative" else selected
    monkeypatch.chdir(checkout.parent)
    effective = configuration.load_configuration(checkout, {}, config_path=config_path)
    assert effective == settings
    overridden = configuration.load_configuration(
        checkout,
        {"GRAFANA_PORT": "4555", "PROMETHEUS_PORT": "18190", "TAPO_PLUG_DEVICES": "192.0.2.81"},
        config_path=config_path,
    )
    assert overridden["grafana"] == {"bind_address": "127.0.0.1", "port": 4555}
    native = overridden["exporter"]
    assert isinstance(native, dict)
    assert native["prometheus_port"] == 18190
    assert native["exporters"]["tapo"]["devices"] == ["192.0.2.81"]
    assert selected.read_text(encoding="utf-8") == content
    assert default.read_bytes() == original
    assert not (checkout / ".runtime").exists()


def test_explicit_source_does_not_read_default_or_legacy_config(checkout: Path, settings: Settings) -> None:
    """Only the selected YAML document participates, even when conventional files are malformed."""
    selected = checkout / "selected stack.yaml"
    selected.write_text(yaml.safe_dump(settings), encoding="utf-8")
    for name in ("stack.yaml", "exporter.yaml"):
        (checkout / "config" / name).write_text("invalid YAML: [\n", encoding="utf-8")
    assert configuration.load_configuration(checkout, {}, config_path=selected) == settings


@pytest.mark.parametrize("selected_content", ["invalid YAML: [\n", "grafana: {port: 3333}\n"])
def test_invalid_or_partial_selection_cannot_merge_with_default(checkout: Path, selected_content: str) -> None:
    """Valid defaults cannot conceal malformed or incomplete explicit configuration."""
    default = checkout / "config" / "stack.yaml"
    original = default.read_bytes()
    selected = checkout / "selected stack.yaml"
    selected.write_text(selected_content, encoding="utf-8")
    with pytest.raises(ValueError, match=r"YAML|mapping|settings"):
        configuration.load_configuration(checkout, {}, config_path=selected)
    assert default.read_bytes() == original
    assert selected.read_text(encoding="utf-8") == selected_content


def test_missing_selection_never_uses_available_default(checkout: Path) -> None:
    """Explicit path typos fail instead of silently switching the monitored fleet."""
    assert configuration.load_configuration(checkout, {})
    with pytest.raises(FileNotFoundError):
        configuration.load_configuration(checkout, {}, config_path=Path("missing stack.yaml"))
    assert not (checkout / ".runtime").exists()


def test_explicit_native_exporter_schema_explains_required_wrapper(checkout: Path, settings: Settings) -> None:
    """Legacy exporter-only files receive migration guidance without implicit wrapping or merging."""
    selected = checkout / "config" / "exporter.yaml"
    content = yaml.safe_dump(settings["exporter"])
    selected.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="native exporter schema") as error:
        configuration.load_configuration(checkout, {}, config_path=selected)
    message = str(error.value).lower()
    assert "native exporter" in message
    assert "schema" in message
    assert "section" in message
    assert selected.read_text(encoding="utf-8") == content
