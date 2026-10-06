# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Verify unified configuration using published images and the real CLI."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from grafana_tp_link import cli as stack_cli
from grafana_tp_link.configuration import OVERRIDE_NAMES, compose_environment, load_configuration, render_configuration
from tests.integration.support import FIXTURES, TEST_PASSWORD, create_stack, http, mapping, run, wait_for

if TYPE_CHECKING:
    from tests.integration.support import Stack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_STACK_INTEGRATION") != "1", reason="Set RUN_STACK_INTEGRATION=1 to start Docker"
    ),
]

COMMENTED_CONFIG = """# Device comments and disabled entries must survive loading.
exporters:
  tapo:
    devices:
      - 192.0.2.10  # Desk fixture
      # - 192.0.2.99  # Disabled fixture
      - 192.0.2.11  # Kitchen fixture
    discovery_options:
      perform_discovery: false
"""

CONFIG_LOADER = """
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from pyprom_exporters.prom_exporter import load_app_config, apply_env_overrides

mounted, _, _ = load_app_config("/etc/pyprom-exporters/config.yaml")
apply_env_overrides(mounted)
with TemporaryDirectory() as directory:
    path = Path(directory) / "devices.yaml"
    path.write_text(sys.stdin.read())
    original = path.read_bytes()
    results = []
    for override in (None, "", "192.0.2.20, 192.0.2.21"):
        if override is None:
            os.environ.pop("TAPO_PLUG_DEVICES", None)
        else:
            os.environ["TAPO_PLUG_DEVICES"] = override
        config, _, exists = load_app_config(str(path))
        apply_env_overrides(config)
        results.append({"exists": exists, "devices": config.exporters.tapo.devices})
    print(json.dumps({"results": results, "unchanged": path.read_bytes() == original,
                      "mounted_port": mounted.prometheus_port, "mounted_log_level": mounted.log_level}))
"""

OVERRIDE_KEYS = {
    *OVERRIDE_NAMES,
    "GRAFANA_ADMIN_USER",
    "GRAFANA_ADMIN_PASSWORD",
    "PYPROM_RUNTIME_DIR",
    "TP_LINK_USERNAME",
    "TP_LINK_PASSWORD",
}


def verify_published_image_device_configuration(stack: Stack) -> None:
    """Exercise pure native configuration loading without creating or querying any device."""
    output = stack.compose("exec", "-T", "exporter", "python", "-c", CONFIG_LOADER, input_text=COMMENTED_CONFIG)
    native = mapping(yaml.safe_load(stack.exporter_config.read_text()))
    assert json.loads(output) == {
        "results": [
            {"exists": True, "devices": ["192.0.2.10", "192.0.2.11"]},
            {"exists": True, "devices": ["192.0.2.10", "192.0.2.11"]},
            {"exists": True, "devices": ["192.0.2.20", "192.0.2.21"]},
        ],
        "unchanged": True,
        "mounted_port": native["prometheus_port"],
        "mounted_log_level": native["log_level"],
    }


def prepare_cli_stack(tmp_path: Path) -> tuple[Stack, dict[str, str]]:
    """Prepare a real CLI checkout whose exporter serves fixtures without probing devices.

    Returns
    -------
    tuple[Stack, dict[str, str]]
        Owned project and environment cleared of ambient runtime overrides.
    """
    instance = create_stack(tmp_path)
    model = mapping(yaml.safe_load((tmp_path / "compose.yaml").read_text()))
    model["name"] = instance.project
    services = mapping(model["services"])
    exporter = mapping(services["exporter"])
    exporter["entrypoint"] = ["python", "/fixtures/metrics_server.py"]
    mounts = exporter["volumes"]
    assert isinstance(mounts, list)
    mounts.append({"type": "bind", "source": str(FIXTURES), "target": "/fixtures", "read_only": True})
    for name in ("exporter", "prometheus", "grafana"):
        service = mapping(services[name])
        service["restart"] = "no"
        service["networks"] = {"default": None}
    for name, port in (("grafana", 3000), ("prometheus", 9090)):
        service = mapping(services[name])
        service["networks"] = {"default": None, "gateway": None}
        service["ports"] = [{"target": port, "published": "0", "host_ip": "127.0.0.1", "protocol": "tcp"}]
    model["networks"] = {"default": {"internal": True}, "gateway": {}}
    (tmp_path / "compose.yaml").write_text(yaml.safe_dump(model, sort_keys=False))
    config_file = tmp_path / "config/stack.yaml"
    configuration = mapping(yaml.safe_load(config_file.read_text()))
    native = mapping(configuration["exporter"])
    mapping(mapping(native["exporters"])["tapo"])["devices"] = ["192.0.2.10", "192.0.2.11"]
    text = yaml.safe_dump(configuration, sort_keys=False)
    config_file.write_text(text.replace("devices:\n", "devices:  # Documented fixture devices\n", 1))
    environment = {key: value for key, value in os.environ.items() if key not in OVERRIDE_KEYS}
    environment.update({"COMPOSE_PROJECT_NAME": instance.project, "GRAFANA_ADMIN_USER": "admin"})
    return instance, environment


def cli(
    instance: Stack,
    environment: dict[str, str],
    command: str,
    *arguments: str,
    config: Path | None = None,
) -> str:
    """Run the installed CLI against the disposable checkout and selected project.

    Returns
    -------
    str
        Combined process output, including startup diagnostics.
    """
    selection = ["--config", str(config)] if config is not None else []
    return run(
        [
            str(Path(sys.executable).with_name("power-monitor")),
            "--directory",
            str(instance.directory),
            *selection,
            command,
            *arguments,
        ],
        environment=environment,
    )


def new_runtime(instance: Stack, existing: set[Path]) -> Path:
    """Require one new immutable runtime directory after a configuration change.

    Returns
    -------
    Path
        Newly rendered native configuration directory.
    """
    created = set((instance.directory / ".runtime").iterdir()) - existing
    assert len(created) == 1, created
    return created.pop()


def verify_rendered_configuration(runtime: Path, *, port: int, level: str) -> None:
    """Check native configuration consistency and exclusion of environment secrets."""
    exporter = mapping(yaml.safe_load((runtime / "exporter.yaml").read_text()))
    assert exporter["prometheus_port"] == port
    assert exporter["log_level"] == level
    assert mapping(mapping(exporter["exporters"])["tapo"])["devices"] == ["192.0.2.10", "192.0.2.11"]
    prometheus = mapping(yaml.safe_load((runtime / "prometheus.yml").read_text()))
    jobs = prometheus["scrape_configs"]
    assert isinstance(jobs, list)
    scrape = next(mapping(job) for job in jobs if mapping(job)["job_name"] == "pyprom-exporters")
    assert scrape["static_configs"] == [{"targets": [f"exporter:{port}"]}]
    datasource = mapping(yaml.safe_load((runtime / "datasource.yaml").read_text()))
    sources = datasource["datasources"]
    assert isinstance(sources, list)
    assert mapping(mapping(sources[0])["jsonData"])["timeInterval"] == mapping(prometheus["global"])["scrape_interval"]
    assert {path.name for path in runtime.iterdir()} == {
        "exporter.yaml",
        "prometheus.yml",
        "datasource.yaml",
        "compose.storage.yaml",
    }
    for path in runtime.iterdir():
        assert TEST_PASSWORD not in path.read_text()
        assert "fixture-login-only" not in path.read_text()


def write_environment(directory: Path, extra: str = "") -> str:
    """Write only fictional credentials and explicit test overrides.

    Returns
    -------
    str
        Expected environment file content for preservation checks.
    """
    content = (
        f"GRAFANA_ADMIN_PASSWORD={TEST_PASSWORD}\nTP_LINK_USERNAME=fixture-login-only\n"
        f"TP_LINK_PASSWORD={TEST_PASSWORD}\nTAPO_PLUG_DEVICES=\n{extra}"
    )
    path = directory / ".env"
    path.write_text(content)
    path.chmod(0o600)
    return content


def test_check_uses_yaml_then_environment_then_shell_without_starting_exporter(tmp_path: Path) -> None:
    """Real Compose interpolation preserves precedence and check starts only promtool."""
    instance, environment = prepare_cli_stack(tmp_path)
    model = mapping(yaml.safe_load((tmp_path / "compose.yaml").read_text()))
    prometheus = mapping(mapping(model["services"])["prometheus"])
    prometheus.pop("networks", None)
    prometheus["network_mode"] = "none"
    (tmp_path / "compose.yaml").write_text(yaml.safe_dump(model, sort_keys=False))
    config_file = tmp_path / "config/stack.yaml"
    original = config_file.read_bytes()
    started_at = str(int(time.time()) - 1)
    try:
        for env_extra, shell_extra, port, level in (
            ("", {}, 18090, "WARNING"),
            ("PROMETHEUS_PORT=18091\nPYPROM_EXPORTERS_LOG_LEVEL=INFO\n", {}, 18091, "INFO"),
            (
                "PROMETHEUS_PORT=18091\nPYPROM_EXPORTERS_LOG_LEVEL=INFO\n",
                {"PROMETHEUS_PORT": "18092", "PYPROM_EXPORTERS_LOG_LEVEL": "ERROR"},
                18092,
                "ERROR",
            ),
        ):
            env_content = write_environment(tmp_path, env_extra)
            existing = set((tmp_path / ".runtime").iterdir())
            cli(instance, environment | shell_extra, "check")
            verify_rendered_configuration(new_runtime(instance, existing), port=port, level=level)
            assert (tmp_path / ".env").read_text() == env_content
        events = run(
            [
                instance.docker,
                "events",
                "--since",
                started_at,
                "--until",
                str(int(time.time()) + 1),
                "--filter",
                "type=container",
                "--filter",
                f"label=com.docker.compose.project={instance.project}",
                "--format",
                '{{index .Actor.Attributes "com.docker.compose.service"}}',
            ]
        )
        assert set(events.splitlines()) == {"prometheus"}
        assert config_file.read_bytes() == original
    finally:
        instance.compose("down", "--volumes", "--remove-orphans", "--timeout", "10")


def runtime_mount(instance: Stack) -> Path:
    """Read the running exporter's mounted native configuration directory.

    Returns
    -------
    Path
        Runtime directory selected by the actual CLI invocation.
    """
    container = instance.compose("ps", "--quiet", "exporter").strip()
    mounts = json.loads(run([instance.docker, "inspect", "--format", "{{json .Mounts}}", container]))
    native = next(
        mapping(mount) for mount in mounts if mapping(mount)["Destination"] == "/etc/pyprom-exporters/config.yaml"
    )
    runtime = Path(str(native["Source"])).parent
    assert runtime.parent == instance.directory / ".runtime"
    return runtime


def project_volumes(instance: Stack) -> set[str]:
    """List only volumes owned by the selected integration project.

    Returns
    -------
    set[str]
        The selected project's persistent storage names.
    """
    return set(
        run(
            [
                instance.docker,
                "volume",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={instance.project}",
                "--format",
                "{{.Name}}",
            ]
        ).splitlines()
    )


def exporter_target_is_healthy(origin: str, port: int) -> bool:
    """Check the actual Prometheus scrape target after a configuration change.

    Returns
    -------
    bool
        Whether the expected exporter endpoint is being scraped successfully.
    """
    response = mapping(json.loads(http(f"{origin}/api/v1/targets?state=active")))
    targets = mapping(response["data"])["activeTargets"]
    assert isinstance(targets, list)
    return any(
        mapping(target)["scrapeUrl"] == f"http://exporter:{port}/metrics" and mapping(target)["health"] == "up"
        for target in targets
    )


def verify_startup_output(instance: Stack, output: str) -> None:
    """Check progress and actual published URLs without exposing fixture credentials."""
    progress = (
        "Resolving environment overrides...",
        f"Loading configuration: {instance.directory / 'config/stack.yaml'}",
        "Rendering service configuration...",
        "Validating Compose configuration...",
        "Starting services and waiting for health checks (up to 120s)...",
        "Services passed Compose health checks.",
    )
    positions = [output.index(message) for message in progress]
    assert positions == sorted(positions)
    grafana = instance.url("grafana", 3000)
    prometheus = instance.url("prometheus", 9090)
    assert f"{grafana}/d/tp-link-power/" in output
    assert f"{prometheus}/targets" in output
    for service in ("Grafana", "Prometheus", "Exporter"):
        assert f"{service}: running, healthy" in output
    assert TEST_PASSWORD not in output
    assert "fixture-login-only" not in output


def test_up_recreates_containers_after_yaml_settings_change(tmp_path: Path) -> None:
    """A second up loads a new runtime digest and effective exporter settings."""
    instance, environment = prepare_cli_stack(tmp_path)
    env_content = write_environment(tmp_path)
    try:
        verify_startup_output(instance, cli(instance, environment, "up"))
        original_container = instance.compose("ps", "--quiet", "exporter").strip()
        original_runtime = runtime_mount(instance)
        verify_rendered_configuration(original_runtime, port=18090, level="WARNING")
        volumes_before = project_volumes(instance)
        assert len(volumes_before) == 2
        grafana = instance.url("grafana", 3000)
        http(f"{grafana}/api/user/preferences", grafana=True, payload={"theme": "light", "timezone": "utc"})
        config_file = tmp_path / "config/stack.yaml"
        config = mapping(yaml.safe_load(config_file.read_text()))
        mapping(config["exporter"]).update({"prometheus_port": 18093, "log_level": "ERROR"})
        mapping(config["prometheus"])["scrape_interval"] = "45s"
        config_file.write_text(yaml.safe_dump(config, sort_keys=False))
        changed = config_file.read_bytes()
        verify_startup_output(instance, cli(instance, environment, "up"))
        assert instance.compose("ps", "--quiet", "exporter").strip() != original_container
        current_runtime = runtime_mount(instance)
        assert current_runtime != original_runtime
        verify_rendered_configuration(current_runtime, port=18093, level="ERROR")
        origin = instance.url("prometheus", 9090)
        status = mapping(json.loads(http(f"{origin}/api/v1/status/config")))
        loaded = mapping(yaml.safe_load(str(mapping(status["data"])["yaml"])))
        assert mapping(loaded["global"])["scrape_interval"] == "45s"
        wait_for(lambda: exporter_target_is_healthy(origin, 18093))
        assert project_volumes(instance) == volumes_before
        grafana = instance.url("grafana", 3000)
        source = mapping(json.loads(http(f"{grafana}/api/datasources/uid/prometheus", grafana=True)))
        assert mapping(source["jsonData"])["timeInterval"] == "45s"
        assert mapping(json.loads(http(f"{grafana}/api/user/preferences", grafana=True)))["theme"] == "light"
        assert config_file.read_bytes() == changed
        assert (tmp_path / ".env").read_text() == env_content
        assert original_runtime.is_dir()
    finally:
        instance.compose("down", "--volumes", "--remove-orphans", "--timeout", "10")


@pytest.mark.parametrize("source", ["dotenv", "shell", "empty-shell", "compose-override"])
def test_custom_credential_keys_resolve_privately_with_compose_precedence(tmp_path: Path, source: str) -> None:
    """Resolve fake credentials through real Compose without starting containers or devices."""
    instance, environment = prepare_cli_stack(tmp_path)
    config_file = tmp_path / "config/stack.yaml"
    config = mapping(yaml.safe_load(config_file.read_text()))
    tapo = mapping(mapping(mapping(config["exporter"])["exporters"])["tapo"])
    discovery = mapping(tapo["discovery_options"])
    discovery.update(
        {
            "tapo_username_env_key": "AUDIT_TAPO_USER",
            "tapo_password_env_key": "AUDIT_TAPO_PASSWORD",  # pragma: allowlist secret
        }
    )
    config_file.write_text(yaml.safe_dump(config))
    for key in ("AUDIT_TAPO_USER", "AUDIT_TAPO_PASSWORD"):
        environment.pop(key, None)
    write_environment(
        tmp_path,
        "AUDIT_TAPO_USER=fixture-user\nAUDIT_TAPO_PASSWORD='fixture$dotenv#credential'\n",  # pragma: allowlist secret
    )
    effective = load_configuration(tmp_path, {})
    runtime = render_configuration(tmp_path, effective)
    environment.update(compose_environment(effective, runtime))
    expected = "fixture$dotenv#credential"
    if source in {"shell", "empty-shell", "compose-override"}:
        expected = "" if source == "empty-shell" else "fixture$shell#credential"
        environment["AUDIT_TAPO_PASSWORD"] = expected
    command = [
        instance.docker,
        "compose",
        "--env-file",
        str(tmp_path / ".env"),
        "-f",
        str(tmp_path / "compose.yaml"),
        "-f",
        str(runtime / "compose.storage.yaml"),
    ]
    if source == "compose-override":
        override = tmp_path / "override.yaml"
        override.write_text("services:\n  exporter:\n    environment:\n      AUDIT_TAPO_PASSWORD: fixture-override\n")
        command.extend(["-f", str(override)])
        expected = "fixture-override"
    model = mapping(json.loads(run([*command, "config", "--format", "json"], environment=environment)))
    exporter = mapping(mapping(model["services"])["exporter"])
    credentials = mapping(exporter["environment"])
    assert credentials["AUDIT_TAPO_USER"] == "fixture-user"
    # Serialized Compose models escape dollars so a second load preserves the literal value.
    assert credentials["AUDIT_TAPO_PASSWORD"] == expected.replace("$", "$$")
    assert all("fixture" not in path.read_text() for path in runtime.iterdir())
    assert "fixture" not in config_file.read_text()


@pytest.mark.parametrize("source", ["dotenv", "shell"])
@pytest.mark.parametrize("suffix", ["$cache", "$$cache", "${AUDIT_STORAGE_SUFFIX}"])
def test_environment_storage_paths_preserve_literal_dollars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, suffix: str
) -> None:
    """Resolve and re-render real Compose overrides without changing literal host-directory names."""
    instance, environment = prepare_cli_stack(tmp_path)
    for name in (*OVERRIDE_NAMES, "GRAFANA_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("cache", "must-not-expand")
    monkeypatch.setenv("AUDIT_STORAGE_SUFFIX", "must-not-expand")
    paths = {
        "GRAFANA_DATA_DIRECTORY": f"grafana data {suffix}",
        "PROMETHEUS_DATA_DIRECTORY": str(tmp_path / f"prometheus data {suffix}"),
    }
    values = paths if source == "dotenv" else dict.fromkeys(paths, "unused dotenv path")
    content = write_environment(tmp_path, "".join(f"{name}='{value}'\n" for name, value in values.items()))
    if source == "shell":
        for name, value in paths.items():
            monkeypatch.setenv(name, value)
    command = [
        instance.docker,
        "compose",
        "--project-directory",
        str(tmp_path),
        "--env-file",
        str(tmp_path / ".env"),
    ]
    status, overrides = stack_cli._resolve_overrides(command, tmp_path)
    assert status == 0
    assert {name: overrides[name] for name in paths} == paths
    effective = load_configuration(tmp_path, overrides)
    runtime = render_configuration(tmp_path, effective)
    environment.update(compose_environment(effective, runtime))
    model = mapping(
        json.loads(
            run(
                [
                    *command,
                    "-f",
                    str(tmp_path / "compose.yaml"),
                    "-f",
                    str(runtime / "compose.storage.yaml"),
                    "config",
                    "--format",
                    "json",
                ],
                environment=environment,
            )
        )
    )
    services = mapping(model["services"])
    for service, target in (("grafana", "/var/lib/grafana"), ("prometheus", "/prometheus")):
        volumes = mapping(services[service])["volumes"]
        assert isinstance(volumes, list)
        mount = next(mapping(volume) for volume in volumes if mapping(volume)["target"] == target)
        expected = str(tmp_path / paths[f"{service.upper()}_DATA_DIRECTORY"])
        # The final serialized model must escape each original dollar exactly once.
        assert mount["source"] == expected.replace("$", "$$")
        assert mapping(mount["bind"])["create_host_path"] is False
        assert not Path(expected).exists()
    assert (tmp_path / ".env").read_text() == content
