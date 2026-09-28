# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Operate the checked-out Compose stack, preserving data unless reset is explicitly requested."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess  # nosec B404 # ruff: ignore[suspicious-subprocess-import]
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from grafana_tp_link.configuration import (
    OVERRIDE_NAMES,
    compose_environment,
    load_configuration,
    render_configuration,
)

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
    parser.add_argument(
        "--compose-file",
        type=Path,
        action="append",
        default=[],
        help="Additional Compose override file, relative to the checkout (repeatable)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create a private .env once; preserve any existing configuration")
    commands.add_parser("check", help="Validate Compose and Prometheus configuration without probing devices")
    commands.add_parser("up", help="Start services and wait for health checks")
    commands.add_parser("down", help="Stop this stack and preserve its persistent data volumes")
    reset = commands.add_parser(
        "reset",
        help="Delete this stack's containers and stored Grafana/Prometheus data",
        description=(
            "Delete this Compose project's containers, networks and managed data volumes. "
            "Keep local configuration, images, external volumes and host directories. Run up to start fresh."
        ),
    )
    reset.add_argument(
        "--yes", action="store_true", required=True, help="Confirm permanent deletion of this stack's data"
    )
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
    sys.stdout.write("Edit settings in config/stack.yaml and credentials in .env before starting services.\n")


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


def _resolve_overrides(compose: list[str], directory: Path) -> tuple[int, dict[str, str]]:
    """Use Compose's own dotenv and shell precedence without displaying values.

    Returns
    -------
    tuple[int, dict[str, str]]
        Compose's exit status and supported noncredential environment overrides.

    Raises
    ------
    ValueError
        If Compose returns an unexpected configuration structure.
    """
    probe = {
        "name": "power-monitor-settings",
        "services": {
            "settings": {
                "image": "scratch",
                "environment": {name: "${" + name + ":-}" for name in OVERRIDE_NAMES},
            }
        },
    }
    # This file contains variable references only. Config resolution never starts a container.
    with TemporaryDirectory(prefix="power-monitor-settings-") as temporary:
        path = Path(temporary) / "compose.yaml"
        path.write_text(yaml.safe_dump(probe), encoding="utf-8")
        result = subprocess.run(  # nosec B603 # ruff: ignore[subprocess-without-shell-equals-true]
            [*compose, "-f", str(path), "config", "--format", "json"],
            cwd=directory,
            check=False,
            capture_output=True,
            text=True,
        )
    if result.returncode:
        sys.stderr.write(
            f"Compose configuration failed (exit status {result.returncode}). Check .env and shell settings.\n"
        )
        return result.returncode, {}
    try:
        environment = json.loads(result.stdout)["services"]["settings"]["environment"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        message = "Compose did not return the expected settings environment configuration."
        raise ValueError(message) from error
    if not isinstance(environment, dict) or any(
        environment.get(name) is not None and not isinstance(environment[name], str) for name in OVERRIDE_NAMES
    ):
        message = "Compose did not return the expected settings environment configuration."
        raise ValueError(message)
    return 0, {name: environment.get(name) or "" for name in OVERRIDE_NAMES}


def _validate_configuration(compose: list[str], directory: Path, *, environment: dict[str, str]) -> int:
    """Validate the final Compose model without exposing resolved credentials.

    Returns
    -------
    int
        Compose's configuration validation exit status.
    """
    result = subprocess.run(  # nosec B603 # ruff: ignore[subprocess-without-shell-equals-true]
        [*compose, "config", "--quiet"],
        cwd=directory,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        sys.stderr.write(
            f"Compose configuration failed (exit status {result.returncode}). "
            "Check compose.yaml, override files and .env, including GRAFANA_ADMIN_PASSWORD.\n"
        )
    return result.returncode


def _check_prometheus(compose: list[str], directory: Path, *, environment: dict[str, str]) -> int:
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
        f"{directory / 'prometheus/tests'}:/etc/prometheus/tests:ro",
        "--entrypoint",
        "promtool",
        "prometheus",
    ]
    for operation in (
        ["check", "config", "/etc/prometheus/prometheus.yml"],
        ["test", "rules", "/etc/prometheus/tests/alerts.test.yml"],
    ):
        status = _run([*promtool, *operation], directory, environment=environment)
        if status:
            return status
    return 0


def _compose_files(paths: list[Path], directory: Path) -> list[str]:
    """Select the base model followed by explicitly ordered overrides.

    Returns
    -------
    list[str]
        Compose file arguments.

    Raises
    ------
    FileNotFoundError
        If an additional Compose file does not exist.
    """
    files = ["-f", "compose.yaml"]
    for override in paths:
        path = (directory / override).resolve()
        if not path.is_file():
            message = f"Compose override file does not exist: {path}"
            raise FileNotFoundError(message)
        files.extend(["-f", str(path)])
    return files


def _lifecycle_arguments(arguments: argparse.Namespace) -> list[str]:
    """Translate a parsed lifecycle command into a fixed Docker argument list.

    Returns
    -------
    list[str]
        Lifecycle operation and explicit options.
    """
    if arguments.command == "logs":
        extra = ["logs", "--tail", "100"]
        if arguments.follow:
            extra.append("--follow")
        return [*extra, *arguments.services]
    return {
        "up": ["up", "-d", "--wait", "--wait-timeout", "120"],
        "down": ["down"],
        "reset": ["down", "--volumes", "--remove-orphans"],
        "status": ["ps"],
        "pull": ["pull"],
    }[arguments.command]


def _compose(arguments: argparse.Namespace, directory: Path) -> int:
    """Resolve settings and run a lifecycle command against this checkout.

    Returns
    -------
    int
        The first failing command's exit status, or zero on success.

    Raises
    ------
    FileNotFoundError
        If Docker, an override file, or required local credentials file is unavailable.
    """
    docker = shutil.which("docker")
    if docker is None:
        message = "Docker with the Compose plugin is required."
        raise FileNotFoundError(message)
    command = arguments.command
    creates_containers = command in {"check", "up"}
    env_file = directory / ".env"
    if creates_containers and not env_file.is_file():
        message = "Missing .env; run 'uv run power-monitor init' and configure credentials first."
        raise FileNotFoundError(message)
    compose = [
        docker,
        "compose",
        "--project-directory",
        str(directory),
        "--env-file",
        str(env_file) if env_file.is_file() else os.devnull,
    ]
    files = _compose_files(arguments.compose_file, directory)
    if creates_containers:
        status, overrides = _resolve_overrides(compose, directory)
        if status:
            return status
        configuration = load_configuration(directory, overrides)
        runtime = render_configuration(directory, configuration)
        environment = os.environ | compose_environment(configuration, runtime)
        active = [name for name, value in overrides.items() if value]
        if active:
            sys.stdout.write(f"Environment overrides: {', '.join(active)}.\n")
    else:
        # Recovery commands need only project identity, even if YAML or local credentials are missing.
        # These placeholders stay in the child environment and cannot create containers.
        environment = os.environ | {
            "TAPO_PLUG_DEVICES": "127.0.0.1",
            "GRAFANA_ADMIN_PASSWORD": secrets.token_urlsafe(32),
            "GRAFANA_BIND_ADDRESS": "127.0.0.1",
            "GRAFANA_PORT": "3000",
            "PYPROM_RUNTIME_DIR": str(directory / ".runtime" / "unconfigured"),
        }
    compose.extend(files)
    if creates_containers:
        status = _validate_configuration(compose, directory, environment=environment)
        if status:
            return status
    if command == "check":
        return _check_prometheus(compose, directory, environment=environment)
    return _run([*compose, *_lifecycle_arguments(arguments)], directory, environment=environment)


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
    except (OSError, ValueError, TypeError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    except KeyboardInterrupt:
        return 130
