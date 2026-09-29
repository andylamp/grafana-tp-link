# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Configuration and lifecycle regressions for the local command wrapper."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess  # nosec B404 # ruff: ignore[suspicious-subprocess-import]
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
import yaml

from grafana_tp_link import cli

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Create a checkout path containing spaces to exercise argument handling.

    Returns
    -------
    Path
        Minimal stack directory with a credential-free initialization template.
    """
    directory = tmp_path / "power monitor"
    directory.mkdir()
    (directory / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    for name in ("config", "prometheus", "grafana"):
        shutil.copytree(ROOT / name, directory / name)
    config = directory / "config" / "stack.yaml"
    model = yaml.safe_load(config.read_text(encoding="utf-8"))
    model["grafana"]["bind_address"] = "127.0.0.1"
    model["exporter"]["exporters"]["tapo"]["devices"] = []
    model["exporter"]["exporters"]["tapo"]["max_concurrent_devices"] = 10
    config.write_text(yaml.safe_dump(model), encoding="utf-8")
    (directory / ".env.example").write_text("GRAFANA_ADMIN_PASSWORD=\nTP_LINK_USERNAME=\n", encoding="utf-8")
    return directory


def test_initialization_is_private_idempotent_and_does_not_log_credentials(
    checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second initialization preserves edited settings and the generated secret."""
    assert cli.main(["--directory", str(checkout), "init"]) == 0
    env = checkout / ".env"
    settings = env.read_text(encoding="utf-8")
    password = settings.splitlines()[0].partition("=")[2]
    assert len(password) >= 32
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert password not in capsys.readouterr().out
    edited = settings.replace("TP_LINK_USERNAME=", "TP_LINK_USERNAME=test-user")
    env.write_text(edited, encoding="utf-8")
    assert cli.main(["--directory", str(checkout), "init"]) == 0
    assert env.read_text(encoding="utf-8") == edited
    assert password not in capsys.readouterr().out


def test_initialization_does_not_follow_existing_symlink(checkout: Path, tmp_path: Path) -> None:
    """An existing symlink cannot cause init to overwrite a different settings file."""
    destination = tmp_path / "existing.env"
    destination.write_text("keep these settings\n", encoding="utf-8")
    (checkout / ".env").symlink_to(destination)
    assert cli.main(["--directory", str(checkout), "init"]) == 0
    assert destination.read_text(encoding="utf-8") == "keep these settings\n"


def _probe_response(environment: Mapping[str, str | None]) -> str:
    """Build a successful settings probe without including any credential value.

    Returns
    -------
    str
        JSON model with noncredential overrides and a configured-password marker.
    """
    return json.dumps(
        {
            "services": {
                "settings": {
                    "environment": dict(environment),
                    "labels": {cli.GRAFANA_BOOTSTRAP_LABEL: "configured"},
                }
            }
        }
    )


@pytest.fixture
def resolved_configuration(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Return a valid Compose response without accessing Docker.

    Returns
    -------
    Mock
        Captured subprocess runner providing resolved JSON configuration.
    """
    response = _probe_response({"TAPO_PLUG_DEVICES": "192.0.2.1"})
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, response, ""))
    monkeypatch.setattr(cli.subprocess, "run", runner)
    return runner


@pytest.fixture
def command_runner(checkout: Path, monkeypatch: pytest.MonkeyPatch, resolved_configuration: Mock) -> Mock:
    """Record subprocess requests without reaching Docker.

    Returns
    -------
    Mock
        Replacement command runner whose default result is success.
    """
    resolved_configuration.reset_mock()
    assert cli.main(["--directory", str(checkout), "init"]) == 0
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/docker")
    runner = Mock(return_value=0)
    monkeypatch.setattr(cli, "_run", runner)
    monkeypatch.setattr(cli, "report_startup", Mock())
    return runner


def test_down_preserves_volumes_and_only_targets_checkout(checkout: Path, command_runner: Mock) -> None:
    """Stopping cannot prune unrelated containers or request volume removal."""
    assert cli.main(["--directory", str(checkout), "down"]) == 0
    command_runner.assert_called_once()
    arguments, directory = command_runner.call_args.args
    assert directory == checkout
    assert arguments[-1] == "down"
    assert arguments[arguments.index("--project-directory") + 1] == str(checkout)
    assert arguments[arguments.index("--env-file") + 1] == str(checkout / ".env")
    assert not {"--volumes", "-v", "prune", "--remove-orphans"}.intersection(arguments)


def test_invalid_compose_stops_before_promtool(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invalid configuration retains its status without revealing captured credentials."""
    private_marker = "PRIVATE_TEST_VALUE"
    resolved_configuration.return_value = subprocess.CompletedProcess([], 7, private_marker, private_marker)
    assert cli.main(["--directory", str(checkout), "check"]) == 7
    command_runner.assert_not_called()
    resolved_configuration.assert_called_once()
    assert resolved_configuration.call_args.args[0][-3:] == ["config", "--format", "json"]
    output = capsys.readouterr()
    assert "exit status 7" in output.err
    assert private_marker not in output.out + output.err


@pytest.mark.parametrize("command", ["check", "up"])
def test_missing_grafana_password_stops_before_rendering_or_containers(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Absent or empty effective passwords fail with a safe remedy and preserve user files."""
    env_file = checkout / ".env"
    original = env_file.read_bytes()
    private_marker = "PRIVATE_CREDENTIAL_VALUE"
    model = json.loads(_probe_response({"TAPO_PLUG_DEVICES": "192.0.2.1", "TP_LINK_PASSWORD": private_marker}))
    model["services"]["settings"]["labels"][cli.GRAFANA_BOOTSTRAP_LABEL] = ""
    resolved_configuration.return_value.stdout = json.dumps(model)
    assert cli.main(["--directory", str(checkout), command]) == 1
    command_runner.assert_not_called()
    resolved_configuration.assert_called_once()
    assert not (checkout / ".runtime").exists()
    assert env_file.read_bytes() == original
    output = capsys.readouterr()
    assert "GRAFANA_ADMIN_PASSWORD is missing or empty" in output.err
    assert "empty shell value overrides .env" in output.err
    assert "Existing .env was preserved" in output.err
    assert private_marker not in output.out + output.err


@pytest.mark.parametrize("marker", [None, False, [], {}, "PRIVATE_INVALID_MARKER"])
def test_invalid_grafana_password_marker_is_not_reported_as_a_missing_password(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    capsys: pytest.CaptureFixture[str],
    marker: object,
) -> None:
    """Malformed probe output fails privately without giving a misleading credential remedy."""
    model = json.loads(_probe_response({"TAPO_PLUG_DEVICES": "192.0.2.1"}))
    model["services"]["settings"]["labels"][cli.GRAFANA_BOOTSTRAP_LABEL] = marker
    resolved_configuration.return_value.stdout = json.dumps(model)
    assert cli.main(["--directory", str(checkout), "check"]) == 1
    command_runner.assert_not_called()
    assert not (checkout / ".runtime").exists()
    output = capsys.readouterr()
    assert "expected Grafana password presence marker" in output.err
    assert "missing or empty" not in output.err
    assert "PRIVATE_INVALID_MARKER" not in output.out + output.err


def test_check_validates_prometheus_without_starting_exporter(checkout: Path, command_runner: Mock) -> None:
    """Promtool runs without bringing up device-probing dependencies."""
    command_runner.side_effect = [5]
    assert cli.main(["--directory", str(checkout), "check"]) == 5
    assert command_runner.call_count == 1
    command = command_runner.call_args.args[0]
    assert "--no-deps" in command
    assert "promtool" in command
    assert command[-3:] == ["check", "config", "/etc/prometheus/prometheus.yml"]


def test_up_waits_for_service_health(checkout: Path, command_runner: Mock) -> None:
    """Successful startup means Compose's health checks completed."""
    assert cli.main(["--directory", str(checkout), "up"]) == 0
    command = command_runner.call_args.args[0]
    assert "--wait" in command
    assert "--wait-timeout" in command
    assert "--force-recreate" not in command


def test_logs_filters_services_without_a_shell(checkout: Path, command_runner: Mock) -> None:
    """Service selection and follow mode remain separate process arguments."""
    assert cli.main(["--directory", str(checkout), "logs", "--follow", "grafana"]) == 0
    assert command_runner.call_args.args[0][-5:] == ["logs", "--tail", "100", "--follow", "grafana"]


def test_missing_env_does_not_launch_containers(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured checkout cannot silently start an unusable stack."""
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/docker")
    runner = Mock()
    monkeypatch.setattr(cli, "_run", runner)
    assert cli.main(["--directory", str(checkout), "up"]) == 1
    runner.assert_not_called()


def test_missing_docker_has_actionable_error(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing Compose prerequisites produce a normal CLI error."""
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    assert cli.main(["--directory", str(checkout), "up"]) == 1


def test_interrupt_returns_shell_convention(checkout: Path, command_runner: Mock) -> None:
    """Interrupting a followed command exits with the conventional status."""
    command_runner.side_effect = KeyboardInterrupt
    assert cli.main(["--directory", str(checkout), "logs", "grafana"]) == 130


def test_check_runs_alert_regressions(checkout: Path, command_runner: Mock) -> None:
    """Configuration and rule regressions both run without starting fleet monitoring."""
    assert cli.main(["--directory", str(checkout), "check"]) == 0
    assert command_runner.call_count == 2
    command = command_runner.call_args.args[0]
    assert command[-3:] == ["test", "rules", "/etc/prometheus/tests/alerts.test.yml"]
    assert "--no-deps" in command


def test_logs_defaults_to_all_services(checkout: Path, command_runner: Mock) -> None:
    """An omitted service list retains the documented all-service behavior."""
    assert cli.main(["--directory", str(checkout), "logs"]) == 0
    assert command_runner.call_args.args[0][-3:] == ["logs", "--tail", "100"]


@pytest.mark.parametrize("service", ["unknown", "--help"])
def test_invalid_log_service_is_not_forwarded(checkout: Path, command_runner: Mock, service: str) -> None:
    """Service names cannot introduce extra Compose flags or invalid targets."""
    with pytest.raises(SystemExit) as error:
        cli.main(["--directory", str(checkout), "logs", "--", service])
    assert error.value.code == 2
    command_runner.assert_not_called()


@pytest.mark.parametrize("command", ["down", "status", "logs", "pull"])
@pytest.mark.parametrize("has_env", [True, False])
def test_management_ignores_invalid_yaml_and_preserves_project(
    checkout: Path, command_runner: Mock, monkeypatch: pytest.MonkeyPatch, command: str, *, has_env: bool
) -> None:
    """Management remains possible after losing credentials and keeps explicit project identity."""
    env_file = checkout / ".env"
    original = env_file.read_bytes()
    if not has_env:
        env_file.unlink()
    (checkout / "config" / "stack.yaml").write_text("invalid YAML: [\n", encoding="utf-8")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "existing-power-stack")
    monkeypatch.setenv("GRAFANA_ADMIN_PASSWORD", "")
    assert cli.main(["--directory", str(checkout), command]) == 0
    command_runner.assert_called_once()
    arguments = command_runner.call_args.args[0]
    environment = command_runner.call_args.kwargs["environment"]
    assert arguments[arguments.index("--env-file") + 1] == (str(env_file) if has_env else os.devnull)
    assert environment["COMPOSE_PROJECT_NAME"] == "existing-power-stack"
    assert environment["TAPO_PLUG_DEVICES"] == "127.0.0.1"
    assert environment["GRAFANA_ADMIN_PASSWORD"]
    assert not os.environ["GRAFANA_ADMIN_PASSWORD"]
    if has_env:
        assert env_file.read_bytes() == original
    else:
        assert not env_file.exists()
    assert not {"up", "run", "create", "--volumes", "prune"}.intersection(arguments)


def test_management_retains_existing_project_configuration(checkout: Path, command_runner: Mock) -> None:
    """Compose still reads custom project names from an existing environment file."""
    env_file = checkout / ".env"
    # The scanner mistakes concatenated environment names for a base64 token; credentials are empty.
    contents = "COMPOSE_PROJECT_NAME=test\nGRAFANA_ADMIN_PASSWORD=\nTAPO_PLUG_DEVICES=\n"  # pragma: allowlist secret
    env_file.write_text(contents, encoding="utf-8")
    assert cli.main(["--directory", str(checkout), "status"]) == 0
    arguments = command_runner.call_args.args[0]
    assert arguments[arguments.index("--env-file") + 1] == str(env_file)
    assert env_file.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("devices", ["", " , \t, \n", ",,,", None])
def test_empty_resolved_device_list_cannot_launch_containers(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    capsys: pytest.CaptureFixture[str],
    devices: str | None,
) -> None:
    """Validation uses Compose's resolved environment and rejects empty token lists."""
    private_marker = "DO_NOT_PRINT_RESOLVED_VALUES"
    resolved_configuration.return_value.stdout = _probe_response(
        {"TAPO_PLUG_DEVICES": devices, "TP_LINK_PASSWORD": private_marker}
    )
    for command in ("check", "up"):
        assert cli.main(["--directory", str(checkout), command]) == 1
        command_runner.assert_not_called()
        output = capsys.readouterr()
        assert "device" in output.err.lower()
        assert private_marker not in output.out + output.err


@pytest.mark.parametrize("content", ["not json PRIVATE_TEST_VALUE", "{}", "null"])
def test_unexpected_compose_response_is_private_and_stops_startup(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    """Malformed or unsupported Compose output fails closed without logging its body."""
    resolved_configuration.return_value.stdout = content
    assert cli.main(["--directory", str(checkout), "up"]) == 1
    command_runner.assert_not_called()
    output = capsys.readouterr()
    assert "expected settings environment" in output.err
    assert "PRIVATE_TEST_VALUE" not in output.out + output.err


def test_resolved_configuration_is_captured_and_management_environment_is_forwarded(
    checkout: Path, resolved_configuration: Mock
) -> None:
    """Capture both Compose streams; send safe overrides only to the child process."""
    status, overrides = cli._resolve_overrides(["/usr/bin/docker", "compose"], checkout)
    assert status == 0
    assert overrides["TAPO_PLUG_DEVICES"] == "192.0.2.1"
    assert resolved_configuration.call_args.kwargs["capture_output"] is True
    assert resolved_configuration.call_args.kwargs["text"] is True
    environment = {"TAPO_PLUG_DEVICES": "127.0.0.1"}
    assert cli._validate_configuration(["/usr/bin/docker", "compose"], checkout, environment=environment) == 0
    assert resolved_configuration.call_args.kwargs["capture_output"] is True
    assert resolved_configuration.call_args.kwargs["env"] == environment
    assert cli._run(["/usr/bin/docker", "compose", "ps"], checkout, environment=environment) == 0
    assert resolved_configuration.call_args.kwargs["env"] == environment


def test_reset_requires_explicit_confirmation_before_docker(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting confirmation cannot trigger any Docker operation or data removal."""
    with pytest.raises(SystemExit) as error:
        cli.main(["--directory", str(checkout), "reset"])
    assert error.value.code == 2
    assert "--yes" in capsys.readouterr().err
    command_runner.assert_not_called()
    resolved_configuration.assert_not_called()


@pytest.mark.parametrize("has_env", [True, False])
@pytest.mark.parametrize("exit_status", [0, 7])
def test_reset_removes_project_data_and_preserves_configuration(
    checkout: Path,
    command_runner: Mock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    has_env: bool,
    exit_status: int,
) -> None:
    """Reset targets the selected project, retains local files and propagates Docker failures."""
    env_file = checkout / ".env"
    settings = env_file.read_bytes()
    if not has_env:
        env_file.unlink()
    config_file = checkout / "config" / "stack.yaml"
    config_file.write_text("invalid YAML: [\n", encoding="utf-8")
    configuration = config_file.read_bytes()
    legacy_data = checkout / "data" / "legacy.txt"
    legacy_data.parent.mkdir()
    legacy_data.write_text("preserve host data", encoding="utf-8")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "isolated-reset-project")
    command_runner.return_value = exit_status
    assert cli.main(["--directory", str(checkout), "reset", "--yes"]) == exit_status
    command_runner.assert_called_once()
    arguments, directory = command_runner.call_args.args
    assert directory == checkout
    assert arguments[-3:] == ["down", "--volumes", "--remove-orphans"]
    assert arguments[arguments.index("--project-directory") + 1] == str(checkout)
    assert arguments[arguments.index("--env-file") + 1] == (str(env_file) if has_env else os.devnull)
    assert arguments[arguments.index("-f") + 1] == "compose.yaml"
    assert command_runner.call_args.kwargs["environment"]["COMPOSE_PROJECT_NAME"] == "isolated-reset-project"
    assert not {"prune", "--rmi", "up", "rm"}.intersection(arguments)
    assert config_file.read_bytes() == configuration
    assert legacy_data.read_text(encoding="utf-8") == "preserve host data"
    if has_env:
        assert env_file.read_bytes() == settings
    else:
        assert not env_file.exists()


@pytest.mark.parametrize("override", [None, ""])
def test_yaml_devices_allow_startup_and_preserve_comments(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    override: str | None,
) -> None:
    """Unset and empty environment overrides select the editable YAML list."""
    config = checkout / "config" / "stack.yaml"
    content = config.read_text(encoding="utf-8").replace(
        "      devices: []",
        '      devices:\n        - "192.0.2.10"  # Office desk\n'
        '        - "kitchen-plug.lan"  # Coffee machine\n'
        '        # - "192.0.2.12"  # Temporarily disabled',
    )
    config.write_text(content, encoding="utf-8")
    environment = {} if override is None else {"TAPO_PLUG_DEVICES": override}
    resolved_configuration.return_value.stdout = _probe_response(environment)
    for command in ("check", "up"):
        assert cli.main(["--directory", str(checkout), command]) == 0
    assert command_runner.call_count == 3
    assert config.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("override", [" ", ",,,"])
def test_invalid_environment_override_does_not_fall_back_to_yaml(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    override: str,
) -> None:
    """A nonempty override replaces YAML even when it resolves to no hosts."""
    config = checkout / "config" / "stack.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("devices: []", "devices: [192.0.2.10]"), encoding="utf-8"
    )
    resolved_configuration.return_value.stdout = _probe_response({"TAPO_PLUG_DEVICES": override})
    assert cli.main(["--directory", str(checkout), "up"]) == 1
    command_runner.assert_not_called()


def test_yaml_syntax_errors_do_not_expose_configuration_values(
    checkout: Path, command_runner: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Parser diagnostics cannot print private configuration contents."""
    marker = "PRIVATE_TEST_VALUE"
    (checkout / "config" / "stack.yaml").write_text(f"credentials: [{marker}\n", encoding="utf-8")
    assert cli.main(["--directory", str(checkout), "check"]) == 1
    command_runner.assert_not_called()
    output = capsys.readouterr()
    assert "YAML" in output.err
    assert marker not in output.out + output.err


def test_missing_stack_configuration_cannot_launch_containers(checkout: Path, command_runner: Mock) -> None:
    """An environment override does not mask a missing bind-mounted configuration file."""
    (checkout / "config" / "stack.yaml").unlink()
    assert cli.main(["--directory", str(checkout), "up"]) == 1
    command_runner.assert_not_called()


@pytest.mark.parametrize("command", ["check", "up"])
def test_final_compose_failure_stops_before_container_creation(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Errors in the full generated stack retain exit status while keeping captured text private."""
    valid_probe = resolved_configuration.return_value
    private_marker = "PRIVATE_COMPOSE_CONTENTS"
    resolved_configuration.side_effect = [
        valid_probe,
        subprocess.CompletedProcess([], 9, private_marker, private_marker),
    ]
    assert cli.main(["--directory", str(checkout), command]) == 9
    command_runner.assert_not_called()
    assert resolved_configuration.call_count == 2
    assert resolved_configuration.call_args.args[0][-2:] == ["config", "--quiet"]
    output = capsys.readouterr()
    assert "exit status 9" in output.err
    assert private_marker not in output.out + output.err


@pytest.mark.parametrize("command", ["up", "check", "status", "logs", "down", "pull", "reset"])
def test_compose_override_order_is_preserved_for_every_operation(
    checkout: Path, command_runner: Mock, resolved_configuration: Mock, command: str
) -> None:
    """Relative and absolute overrides follow the base model for creation and recovery alike."""
    relative = Path("storage override.yaml")
    absolute = checkout / "last override.yaml"
    for override in (checkout / relative, absolute):
        override.write_text("services: {}\n", encoding="utf-8")
    arguments = [
        "--directory",
        str(checkout),
        "--compose-file",
        str(relative),
        "--compose-file",
        str(absolute),
        command,
    ]
    if command == "reset":
        arguments.append("--yes")
    assert cli.main(arguments) == 0
    expected_files = ["compose.yaml"]
    if command in {"up", "check"}:
        runtime = Path(command_runner.call_args.kwargs["environment"]["PYPROM_RUNTIME_DIR"])
        storage = runtime / "compose.storage.yaml"
        assert storage.is_file()
        expected_files.append(str(storage))
    expected_files.extend([str(checkout / relative), str(absolute)])
    for call in command_runner.call_args_list:
        process_arguments = call.args[0]
        files = [process_arguments[index + 1] for index, value in enumerate(process_arguments) if value == "-f"]
        assert files == expected_files
    if command in {"up", "check"}:
        assert resolved_configuration.call_count == 2
        final_arguments = resolved_configuration.call_args.args[0]
        files = [final_arguments[index + 1] for index, value in enumerate(final_arguments) if value == "-f"]
        assert files == expected_files
    else:
        resolved_configuration.assert_not_called()


@pytest.mark.parametrize("command", ["up", "check", "down", "reset"])
def test_missing_compose_override_fails_before_rendering_or_docker_mutations(
    checkout: Path, command_runner: Mock, resolved_configuration: Mock, command: str
) -> None:
    """A missing custom storage model cannot silently launch or remove the base stack instead."""
    arguments = ["--directory", str(checkout), "--compose-file", "missing.yaml", command]
    if command == "reset":
        arguments.append("--yes")
    assert cli.main(arguments) == 1
    command_runner.assert_not_called()
    resolved_configuration.assert_not_called()
    assert not (checkout / ".runtime").exists()


def test_up_renders_yaml_and_forwards_effective_values_without_changing_source(
    checkout: Path, command_runner: Mock, resolved_configuration: Mock
) -> None:
    """A normal launch uses YAML ports, inventory and timing consistently across all services."""
    config = checkout / "config" / "stack.yaml"
    content = config.read_text(encoding="utf-8").replace("devices: []", "devices: [192.0.2.10]  # Desk plug")
    model = yaml.safe_load(content)
    model["grafana"]["port"] = 4321
    model["exporter"]["prometheus_port"] = 8097
    model["prometheus"]["scrape_interval"] = "45s"
    content = "# Preserve this user comment.\n" + yaml.safe_dump(model)
    config.write_text(content, encoding="utf-8")
    resolved_configuration.return_value.stdout = _probe_response({})
    assert cli.main(["--directory", str(checkout), "up"]) == 0
    environment = command_runner.call_args.kwargs["environment"]
    assert environment["GRAFANA_PORT"] == "4321"
    assert environment["PROMETHEUS_PORT"] == "8097"
    runtime = Path(environment["PYPROM_RUNTIME_DIR"])
    assert runtime.parent == checkout / ".runtime"
    exporter = yaml.safe_load((runtime / "exporter.yaml").read_text(encoding="utf-8"))
    prometheus = yaml.safe_load((runtime / "prometheus.yml").read_text(encoding="utf-8"))
    datasource = yaml.safe_load((runtime / "datasource.yaml").read_text(encoding="utf-8"))
    assert exporter["exporters"]["tapo"]["devices"] == ["192.0.2.10"]
    assert prometheus["global"]["scrape_interval"] == "45s"
    assert prometheus["scrape_configs"][0]["static_configs"][0]["targets"] == ["exporter:8097"]
    assert datasource["datasources"][0]["jsonData"]["timeInterval"] == "45s"
    assert config.read_text(encoding="utf-8") == content
    assert resolved_configuration.call_args.kwargs["env"] == environment


def test_resolved_environment_overrides_yaml_without_logging_values(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resolved overrides change generated settings and report names without exposing their values."""
    overrides = {"TAPO_PLUG_DEVICES": "private-device.example", "GRAFANA_PORT": "4555", "PROMETHEUS_PORT": "8199"}
    resolved_configuration.return_value.stdout = _probe_response(overrides)
    assert cli.main(["--directory", str(checkout), "up"]) == 0
    environment = command_runner.call_args.kwargs["environment"]
    assert environment["GRAFANA_PORT"] == "4555"
    runtime = Path(environment["PYPROM_RUNTIME_DIR"])
    exporter = yaml.safe_load((runtime / "exporter.yaml").read_text(encoding="utf-8"))
    assert exporter["prometheus_port"] == 8199
    assert exporter["exporters"]["tapo"]["devices"] == ["private-device.example"]
    output = capsys.readouterr()
    assert "TAPO_PLUG_DEVICES" in output.out
    assert "private-device.example" not in output.out + output.err


def test_yaml_edits_change_runtime_mounts_for_next_up(checkout: Path, command_runner: Mock) -> None:
    """Changing effective YAML causes a later up to receive new mount paths without force-recreate."""
    assert cli.main(["--directory", str(checkout), "up"]) == 0
    previous_runtime = Path(command_runner.call_args.kwargs["environment"]["PYPROM_RUNTIME_DIR"])
    previous_content = (previous_runtime / "exporter.yaml").read_bytes()
    config = checkout / "config" / "stack.yaml"
    changed = config.read_text(encoding="utf-8").replace("max_concurrent_devices: 10", "max_concurrent_devices: 4")
    config.write_text(changed, encoding="utf-8")
    assert cli.main(["--directory", str(checkout), "up"]) == 0
    runtime = Path(command_runner.call_args.kwargs["environment"]["PYPROM_RUNTIME_DIR"])
    assert runtime != previous_runtime
    assert (previous_runtime / "exporter.yaml").read_bytes() == previous_content
    assert (
        yaml.safe_load((runtime / "exporter.yaml").read_text(encoding="utf-8"))["exporters"]["tapo"][
            "max_concurrent_devices"
        ]
        == 4
    )
    assert config.read_text(encoding="utf-8") == changed


@pytest.fixture
def outside_checkout(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise checkout-relative paths while running from another working directory."""
    monkeypatch.chdir(checkout.parent)


@pytest.mark.usefixtures("outside_checkout")
@pytest.mark.parametrize("path_kind", ["relative", "absolute", "external"])
@pytest.mark.parametrize("command", ["check", "up"])
def test_selected_stack_path_uses_checkout_templates_and_preserves_both_sources(
    checkout: Path,
    command_runner: Mock,
    resolved_configuration: Mock,
    path_kind: str,
    command: str,
) -> None:
    """Explicit stack files resolve against the checkout while generated files and credentials stay there."""
    default = checkout / "config" / "stack.yaml"
    original = default.read_bytes()
    selected = (checkout.parent if path_kind == "external" else checkout) / "custom stack.yaml"
    model = yaml.safe_load(original)
    model["grafana"]["port"] = 4321
    model["exporter"]["prometheus_port"] = 18090
    model["exporter"]["exporters"]["tapo"]["devices"] = ["192.0.2.80"]
    content = "# Preserve alternate device notes.\n" + yaml.safe_dump(model)
    selected.write_text(content, encoding="utf-8")
    argument = str(selected.relative_to(checkout)) if path_kind == "relative" else str(selected)
    resolved_configuration.return_value.stdout = _probe_response({})
    assert cli.main(["--directory", str(checkout), "--config", argument, command]) == 0
    environment = command_runner.call_args.kwargs["environment"]
    assert environment["GRAFANA_PORT"] == "4321"
    assert environment["PROMETHEUS_PORT"] == "18090"
    assert environment["TAPO_PLUG_DEVICES"] == "192.0.2.80"
    runtime = Path(environment["PYPROM_RUNTIME_DIR"])
    assert runtime.parent == checkout / ".runtime"
    native = yaml.safe_load((runtime / "exporter.yaml").read_text(encoding="utf-8"))
    prometheus = yaml.safe_load((runtime / "prometheus.yml").read_text(encoding="utf-8"))
    assert native["exporters"]["tapo"]["devices"] == ["192.0.2.80"]
    assert native["prometheus_port"] == 18090
    assert prometheus["scrape_configs"][0]["static_configs"][0]["targets"] == ["exporter:18090"]
    for call in command_runner.call_args_list:
        arguments, directory = call.args
        assert directory == checkout
        assert arguments[arguments.index("--env-file") + 1] == str(checkout / ".env")
        assert arguments[arguments.index("--project-directory") + 1] == str(checkout)
    assert selected.read_text(encoding="utf-8") == content
    assert default.read_bytes() == original
    assert not (checkout.parent / ".runtime").exists()


def test_resolved_environment_still_overrides_explicit_stack_file(
    checkout: Path, command_runner: Mock, resolved_configuration: Mock
) -> None:
    """Selecting another YAML file does not bypass shell or dotenv override precedence."""
    default = checkout / "config" / "stack.yaml"
    original = default.read_bytes()
    selected = checkout / "selected stack.yaml"
    model = yaml.safe_load(original)
    model["grafana"]["port"] = 4321
    model["exporter"]["prometheus_port"] = 18090
    model["exporter"]["exporters"]["tapo"]["devices"] = ["192.0.2.80"]
    content = "# Preserve selected settings.\n" + yaml.safe_dump(model)
    selected.write_text(content, encoding="utf-8")
    overrides = {"GRAFANA_PORT": "4555", "PROMETHEUS_PORT": "18190", "TAPO_PLUG_DEVICES": "192.0.2.81"}
    resolved_configuration.return_value.stdout = _probe_response(overrides)
    assert cli.main(["--directory", str(checkout), "--config", str(selected), "up"]) == 0
    environment = command_runner.call_args.kwargs["environment"]
    assert all(environment[key] == value for key, value in overrides.items())
    native = yaml.safe_load((Path(environment["PYPROM_RUNTIME_DIR"]) / "exporter.yaml").read_text(encoding="utf-8"))
    assert native["prometheus_port"] == 18190
    assert native["exporters"]["tapo"]["devices"] == ["192.0.2.81"]
    assert selected.read_text(encoding="utf-8") == content
    assert default.read_bytes() == original


@pytest.mark.parametrize("selected_content", [None, "invalid YAML: [\n", "grafana: {port: 3333}\n"])
@pytest.mark.parametrize("command", ["check", "up"])
def test_invalid_explicit_config_never_falls_back_to_valid_default(
    checkout: Path,
    command_runner: Mock,
    selected_content: str | None,
    command: str,
) -> None:
    """Missing, malformed and partial selections cannot silently use the checkout's default settings."""
    default = checkout / "config" / "stack.yaml"
    valid = default.read_text(encoding="utf-8").replace("devices: []", "devices: [192.0.2.10]")
    default.write_text(valid, encoding="utf-8")
    selected = checkout / "selected stack.yaml"
    if selected_content is not None:
        selected.write_text(selected_content, encoding="utf-8")
    assert cli.main(["--directory", str(checkout), "--config", str(selected), command]) == 1
    command_runner.assert_not_called()
    assert not (checkout / ".runtime").exists()
    assert default.read_text(encoding="utf-8") == valid


def test_native_exporter_selection_has_actionable_schema_guidance(
    checkout: Path, command_runner: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exporter-only file cannot masquerade as the unified stack schema."""
    model = yaml.safe_load((checkout / "config" / "stack.yaml").read_text(encoding="utf-8"))
    selected = checkout / "config" / "exporter.yaml"
    selected.write_text(yaml.safe_dump(model["exporter"]), encoding="utf-8")
    assert cli.main(["--directory", str(checkout), "--config", str(selected), "up"]) == 1
    command_runner.assert_not_called()
    output = capsys.readouterr().err.lower()
    assert "native exporter" in output
    assert "schema" in output
    assert "section" in output
    assert not (checkout / ".runtime").exists()


@pytest.mark.parametrize("command", ["down", "reset", "status", "logs", "pull"])
def test_recovery_ignores_missing_selected_config_and_credentials(
    checkout: Path, command_runner: Mock, resolved_configuration: Mock, command: str
) -> None:
    """An explicit configuration path is unnecessary for operations that cannot create containers."""
    (checkout / ".env").unlink()
    (checkout / "config" / "stack.yaml").unlink()
    arguments = ["--directory", str(checkout), "--config", "missing custom stack.yaml", command]
    if command == "reset":
        arguments.append("--yes")
    assert cli.main(arguments) == 0
    command_runner.assert_called_once()
    resolved_configuration.assert_not_called()
    assert not (checkout / ".runtime").exists()
    assert not (checkout / ".env").exists()


def test_up_reports_progress_before_starting_and_summarizes_only_after_health_checks(
    checkout: Path,
    command_runner: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Effective collection settings precede Docker output; URLs follow successful startup."""
    config = checkout / "config" / "stack.yaml"
    model = yaml.safe_load(config.read_text(encoding="utf-8"))
    model["exporter"]["exporters"]["tapo"]["prometheus_options"]["refresh_interval"] = 60
    config.write_text(yaml.safe_dump(model), encoding="utf-8")
    private_marker = "PRIVATE_GRAFANA_PASSWORD"
    monkeypatch.setenv("GRAFANA_ADMIN_PASSWORD", private_marker)
    output_before_start = []

    def start(*_args: object, **_kwargs: object) -> int:
        output_before_start.append(capsys.readouterr().out)
        return 0

    command_runner.side_effect = start
    summary = Mock()
    monkeypatch.setattr(cli, "report_startup", summary)
    assert cli.main(["--directory", str(checkout), "up"]) == 0
    assert len(output_before_start) == 1
    before = output_before_start[0]
    assert "Devices: 1 configured; background polling every 60s." in before
    assert "Prometheus: scrape interval 30s; scrape timeout 25s." in before
    assert before.index("Loading configuration:") < before.index("Rendering service configuration...")
    assert before.index("Validating Compose configuration...") < before.index("Starting services")
    assert "Services passed Compose health checks." not in before
    summary.assert_called_once_with(
        command_runner.call_args.args[0][:-5], checkout, environment=command_runner.call_args.kwargs["environment"]
    )
    after = capsys.readouterr()
    assert "Services passed Compose health checks." in after.out
    assert "power-monitor" in after.out
    assert "logs --follow" in after.out
    assert private_marker not in before + after.out + after.err


@pytest.mark.parametrize("status", [0, 7])
def test_check_reports_only_completed_validation_steps(
    checkout: Path,
    command_runner: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    """Failed configuration checks never print success or advertise running services."""
    summary = Mock()
    monkeypatch.setattr(cli, "report_startup", summary)
    command_runner.return_value = status
    assert cli.main(["--directory", str(checkout), "check"]) == status
    output = capsys.readouterr().out
    assert "live probing on scrape" in output
    assert "Checking Prometheus configuration..." in output
    assert ("Checking alert-rule fixtures..." in output) is (status == 0)
    assert ("Configuration checks passed." in output) is (status == 0)
    assert "Services passed Compose health checks." not in output
    summary.assert_not_called()


def test_failed_up_retains_exit_status_and_prints_quoted_diagnostic_commands(
    checkout: Path,
    command_runner: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Failed health waits offer commands preserving checkout and ordered override files."""
    override = checkout / "override with spaces.yaml"
    override.write_text("services: {}\n", encoding="utf-8")
    summary = Mock()
    monkeypatch.setattr(cli, "report_startup", summary)
    command_runner.return_value = 17
    assert cli.main(["--directory", str(checkout), "--compose-file", override.name, "up"]) == 17
    output = capsys.readouterr()
    assert "Stack startup failed (exit status 17)." in output.err
    assert "Services passed Compose health checks." not in output.out
    assert "http://" not in output.out
    commands = [shlex.split(line) for line in output.out.splitlines() if line.startswith("  uv ")]
    assert len(commands) == 2
    for command in commands:
        assert command[command.index("--project") + 1] == str(checkout)
        assert command[command.index("--directory") + 1] == str(checkout)
        assert command[command.index("--compose-file") + 1] == override.name
    assert commands[0][-1] == "status"
    assert commands[1][-2:] == ["logs", "--follow"]
    summary.assert_not_called()
