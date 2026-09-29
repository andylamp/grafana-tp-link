# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Report service health and published URLs without probing devices or exposing credentials."""

from __future__ import annotations

import ipaddress
import json
import subprocess  # nosec B404 # ruff: ignore[suspicious-subprocess-import]
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_SERVICES = {"grafana": "Grafana", "prometheus": "Prometheus", "exporter": "Exporter"}
_STATES = {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
_HEALTH = {"starting", "healthy", "unhealthy"}
_MAX_PORT = 65535
_STATUS_TIMEOUT = 10


def _containers(output: str) -> list[dict[str, object]]:
    """Decode current JSON Lines and older array-form Compose status.

    Returns
    -------
    list[dict[str, object]]
        Container records; individual fields are validated before display.

    Raises
    ------
    ValueError
        If the response is not a collection of container objects.
    """
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in output.splitlines() if line.strip()]
    records = [parsed] if isinstance(parsed, dict) else parsed
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        message = "Unexpected Compose status format."
        raise ValueError(message)
    return records


def _published_url(publisher: object, target_port: int) -> str | None:
    """Normalize a valid TCP binding to an HTTP URL on the Docker host.

    Returns
    -------
    str or None
        URL for the requested container port, or no match for an invalid/unpublished binding.
    """
    if not isinstance(publisher, dict):
        return None
    port = publisher.get("PublishedPort")
    host = publisher.get("URL")
    if not isinstance(host, str) or "%" in host:
        return None
    if (
        publisher.get("Protocol") != "tcp"
        or publisher.get("TargetPort") != target_port
        or type(port) is not int
        or not 1 <= port <= _MAX_PORT
    ):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        name = "[::1]" if address.is_unspecified else f"[{address}]"
    else:
        name = "127.0.0.1" if address.is_unspecified else str(address)
    return f"http://{name}:{port}"


def _status(record: dict[str, object]) -> str:
    """Select only recognized state and health strings for terminal output.

    Returns
    -------
    str
        Safe service state and health text without arbitrary Docker model values.
    """
    state = record.get("State")
    state = state if isinstance(state, str) and state in _STATES else "unknown state"
    health = record.get("Health")
    if isinstance(health, str) and not health:
        return f"{state}, no health check"
    health = health if isinstance(health, str) and health in _HEALTH else "unknown health"
    return f"{state}, {health}"


def _service_lines(service: str, records: list[dict[str, object]], target_port: int) -> list[str]:
    """Format one recognized service's status and deduplicated published endpoints.

    Returns
    -------
    list[str]
        Readable status and URL lines for the chosen service.
    """
    title = _SERVICES[service]
    if not records:
        return [f"  {title}: status unavailable; run power-monitor status.\n"]
    states = "; ".join(sorted({_status(record) for record in records}))
    lines = [f"  {title}: {states}\n"]
    urls: set[str] = set()
    for record in records:
        publishers = record.get("Publishers")
        if isinstance(publishers, list):
            urls.update(url for publisher in publishers if (url := _published_url(publisher, target_port)))
    if not urls:
        lines.append("    Internal service; no published HTTP port.\n")
    for url in sorted(urls):
        if service == "grafana":
            lines.extend([f"    Home: {url}/\n", f"    Dashboard: {url}/d/tp-link-power/\n"])
        elif service == "prometheus":
            lines.extend([f"    UI: {url}/\n", f"    Scrape targets: {url}/targets\n"])
        else:
            lines.append(f"    Metrics: {url}/metrics\n")
    return lines


def report_startup(compose: list[str], directory: Path, *, environment: dict[str, str]) -> None:
    """Show the running stack's health and actual published ports after successful startup.

    URLs refer to the Docker host, which may be remote. Failures to retrieve the
    optional summary never turn successful startup into a command failure. Only
    known service/status names and validated numeric addresses/ports are displayed;
    captured Compose output can contain credentials and must never be forwarded.
    """
    try:
        result = subprocess.run(  # nosec B603 # ruff: ignore[subprocess-without-shell-equals-true]
            [*compose, "ps", "--all", "--format", "json", *_SERVICES],
            cwd=directory,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_STATUS_TIMEOUT,
        )
        records = _containers(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        records = []
    if not records:
        sys.stderr.write("Startup completed; service details are unavailable. Run power-monitor status for details.\n")
        return
    sys.stdout.write(
        "\nService health and URLs on the Docker host:\n"
        "For a remote Docker context, use that host or an SSH tunnel to reach its published ports.\n"
    )
    try:
        exporter_port = int(environment.get("PROMETHEUS_PORT", "8090"))
    except ValueError:
        exporter_port = 0
    ports = {"grafana": 3000, "prometheus": 9090, "exporter": exporter_port}
    for service in _SERVICES:
        selected = [record for record in records if record.get("Service") == service]
        sys.stdout.writelines(_service_lines(service, selected, ports[service]))
