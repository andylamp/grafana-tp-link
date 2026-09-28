# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Operate the checked-out Compose stack without deleting persistent data."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess  # nosec B404 # ruff: ignore[suspicious-subprocess-import]
import sys
from pathlib import Path

SERVICES = ("exporter", "prometheus", "grafana")


def _parser() -> argparse.ArgumentParser:
    """Build the checkout-oriented command interface.

    Returns
    -------
    argparse.ArgumentParser
        Parser for initialization, validation and service lifecycle commands.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=Path.cwd(), help="Checkout directory (default: current directory)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create a private .env once; preserve any existing configuration")
    commands.add_parser("check", help="Validate Compose and Prometheus configuration without probing devices")
    commands.add_parser("up", help="Start services and wait for health checks")
    commands.add_parser("down", help="Stop this stack and preserve its persistent data volumes")
    commands.add_parser("status", help="Show service status")
    commands.add_parser("pull", help="Download the pinned service images")
    logs = commands.add_parser("logs", help="Show recent logs")
    logs.add_argument("services", nargs="*", metavar="SERVICE", help=f"Optional services: {', '.join(SERVICES)}")
    logs.add_argument("--follow", action="store_true", help="Follow new log output")
    return parser


def _initialize(directory: Path) -> None:
    """Create private initial settings without overwriting an existing file.

    Raises
    ------
    ValueError
        If the example does not contain the expected empty password setting.
    """
    target = directory / ".env"
    template = (directory / ".env.example").read_text(encoding="utf-8")
    placeholder = "GRAFANA_ADMIN_PASSWORD="
    lines = template.splitlines()
    if lines.count(placeholder) != 1:
        message = "The example must contain exactly one empty GRAFANA_ADMIN_PASSWORD setting."
        raise ValueError(message)
    lines[lines.index(placeholder)] = placeholder + secrets.token_urlsafe(32)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        sys.stdout.write("Existing .env preserved.\n")
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    sys.stdout.write("Created .env with private permissions and a generated Grafana password.\n")
    sys.stdout.write("Edit TAPO_PLUG_DEVICES and any required credentials before starting services.\n")


def _run(arguments: list[str], directory: Path, *, environment: dict[str, str] | None = None) -> int:
    """Execute fixed argument lists with inherited terminal output.

    Returns
    -------
    int
        The subprocess exit status.
    """
    # Docker is resolved explicitly; user input is passed as arguments, never shell code.
    result = subprocess.run(  # nosec B603 # ruff: ignore[subprocess-without-shell-equals-true]
        arguments, cwd=directory, check=False, env=environment
    )
    return result.returncode


def _validate_configuration(compose: list[str], directory: Path) -> int:
    """Check effective settings without printing resolved credentials.

    Returns
    -------
    int
        The configuration command's exit status.

    Raises
    ------
    ValueError
        If the resolved configuration has no explicit device hosts or has an unexpected shape.
    """
    result = subprocess.run(  # nosec B603 # ruff: ignore[subprocess-without-shell-equals-true]
        [*compose, "config", "--format", "json"], cwd=directory, check=False, capture_output=True, text=True
    )
    if result.returncode:
        sys.stderr.write(
            f"Compose configuration failed (exit status {result.returncode}). "
            "Check compose.yaml and .env, including TAPO_PLUG_DEVICES and GRAFANA_ADMIN_PASSWORD.\n"
        )
        return result.returncode
    try:
        model = json.loads(result.stdout)
        devices = model["services"]["exporter"]["environment"]["TAPO_PLUG_DEVICES"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        message = "Compose did not return the expected exporter environment configuration."
        raise ValueError(message) from error
    if not isinstance(devices, str) or not devices.replace(",", " ").split():
        message = "Set TAPO_PLUG_DEVICES to at least one explicit device IP or hostname before check/up."
        raise ValueError(message)
    return 0


def _check_prometheus(compose: list[str], directory: Path) -> int:
    """Validate Prometheus and alert fixtures without starting exporter dependencies.

    Returns
    -------
    int
        The first failing validation command's exit status, or zero on success.
    """
    promtool = [
        *compose,
        "run",
        "--rm",
        "--no-deps",
        "--volume",
        f"{directory / 'prometheus'}:/etc/prometheus:ro",
        "--entrypoint",
        "promtool",
        "prometheus",
    ]
    for operation in (
        ["check", "config", "/etc/prometheus/prometheus.yml"],
        ["test", "rules", "/etc/prometheus/tests/alerts.test.yml"],
    ):
        status = _run([*promtool, *operation], directory)
        if status:
            return status
    return 0


def _compose(arguments: argparse.Namespace, directory: Path) -> int:
    """Run the selected lifecycle command against this checkout's stack.

    Returns
    -------
    int
        The first failing command's exit status, or zero on success.

    Raises
    ------
    FileNotFoundError
        If Docker is unavailable or the local environment file has not been created.
    """
    docker = shutil.which("docker")
    if docker is None:
        message = "Docker with the Compose plugin is required."
        raise FileNotFoundError(message)
    command = arguments.command
    creates_containers = command in {"check", "up"}
    env_file = directory / ".env"
    if creates_containers and not env_file.is_file():
        message = "Missing .env; run 'uv run power-monitor init' and configure your devices first."
        raise FileNotFoundError(message)
    compose = [
        docker,
        "compose",
        "--project-directory",
        str(directory),
        "--env-file",
        str(env_file) if env_file.is_file() else os.devnull,
        "-f",
        "compose.yaml",
    ]
    environment = None
    if creates_containers:
        status = _validate_configuration(compose, directory)
        if status:
            return status
    else:
        # These commands cannot start containers. Override required values only in the
        # subprocess, retaining the environment file and COMPOSE_PROJECT_NAME resolution.
        placeholder = "unused-for-management"
        environment = os.environ | {"TAPO_PLUG_DEVICES": "127.0.0.1", "GRAFANA_ADMIN_PASSWORD": placeholder}
    if command == "check":
        return _check_prometheus(compose, directory)
    if command == "logs":
        extra = ["logs", "--tail", "100"]
        if arguments.follow:
            extra.append("--follow")
        extra.extend(arguments.services)
    else:
        extra = {
            "up": ["up", "-d", "--wait", "--wait-timeout", "120"],
            "down": ["down"],
            "status": ["ps"],
            "pull": ["pull"],
        }[command]
    return _run([*compose, *extra], directory, environment=environment)


def main(argv: list[str] | None = None) -> int:
    """Run the local stack utility.

    Parameters
    ----------
    argv : list[str] | None
        Arguments without the program name, or None to use the process arguments.

    Returns
    -------
    int
        Zero on success; configuration and command failures return a nonzero status.
    """
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "logs" and any(service not in SERVICES for service in arguments.services):
        parser.error(f"Choose services from: {', '.join(SERVICES)}.")
    directory = arguments.directory.resolve()
    if not (directory / "compose.yaml").is_file():
        parser.error("Run inside the project checkout or pass --directory /path/to/grafana-tp-link.")
    try:
        if arguments.command == "init":
            _initialize(directory)
            return 0
        return _compose(arguments, directory)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    except KeyboardInterrupt:
        return 130
