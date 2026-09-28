# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Configuration and lifecycle regressions for the local command wrapper."""

from __future__ import annotations

import json
import os
import stat
import subprocess  # nosec B404 # ruff: ignore[suspicious-subprocess-import]
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from grafana_tp_link import cli

if TYPE_CHECKING:
    from pathlib import Path


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
    (directory / ".env.example").write_text("GRAFANA_ADMIN_PASSWORD=\nTAPO_PLUG_DEVICES=\n", encoding="utf-8")
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
    edited = settings.replace("TAPO_PLUG_DEVICES=", "TAPO_PLUG_DEVICES=192.0.2.1")
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


@pytest.fixture
def resolved_configuration(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Return a valid Compose response without accessing Docker.

    Returns
    -------
    Mock
        Captured subprocess runner providing resolved JSON configuration.
    """
    model = {"services": {"exporter": {"environment": {"TAPO_PLUG_DEVICES": "192.0.2.1"}}}}
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(model), ""))
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
def test_management_without_env_preserves_project_and_does_not_write_settings(
    checkout: Path, command_runner: Mock, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Management remains possible after losing credentials and keeps explicit project identity."""
    env_file = checkout / ".env"
    env_file.unlink()
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "existing-power-stack")
    monkeypatch.setenv("GRAFANA_ADMIN_PASSWORD", "")
    assert cli.main(["--directory", str(checkout), command]) == 0
    command_runner.assert_called_once()
    arguments = command_runner.call_args.args[0]
    environment = command_runner.call_args.kwargs["environment"]
    assert arguments[arguments.index("--env-file") + 1] == os.devnull
    assert environment["COMPOSE_PROJECT_NAME"] == "existing-power-stack"
    assert environment["TAPO_PLUG_DEVICES"] == "127.0.0.1"
    assert environment["GRAFANA_ADMIN_PASSWORD"]
    assert not os.environ["GRAFANA_ADMIN_PASSWORD"]
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
    model = {
        "services": {"exporter": {"environment": {"TAPO_PLUG_DEVICES": devices, "TP_LINK_PASSWORD": private_marker}}}
    }
    resolved_configuration.return_value.stdout = json.dumps(model)
    for command in ("check", "up"):
        assert cli.main(["--directory", str(checkout), command]) == 1
        command_runner.assert_not_called()
        output = capsys.readouterr()
        assert "at least one explicit device" in output.err
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
    assert "expected exporter environment" in output.err
    assert "PRIVATE_TEST_VALUE" not in output.out + output.err


def test_resolved_configuration_is_captured_and_management_environment_is_forwarded(
    checkout: Path, resolved_configuration: Mock
) -> None:
    """Capture both Compose streams; send safe overrides only to the child process."""
    assert cli._validate_configuration(["/usr/bin/docker", "compose"], checkout) == 0
    assert resolved_configuration.call_args.kwargs["capture_output"] is True
    assert resolved_configuration.call_args.kwargs["text"] is True
    environment = {"TAPO_PLUG_DEVICES": "127.0.0.1"}
    assert cli._run(["/usr/bin/docker", "compose", "ps"], checkout, environment=environment) == 0
    assert resolved_configuration.call_args.kwargs["env"] == environment
