# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Published-port, status parsing and private diagnostic output regressions."""

from __future__ import annotations

import json
import subprocess  # nosec B404 # ruff: ignore[suspicious-subprocess-import]
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from grafana_tp_link import status_output

if TYPE_CHECKING:
    from pathlib import Path


def _record(service: str, publishers: object = None, **fields: object) -> dict[str, object]:
    """Create a representative Compose container status record.

    Returns
    -------
    dict[str, object]
        Container data with a healthy running status unless explicitly overridden.
    """
    return {"Service": service, "State": "running", "Health": "healthy", "Publishers": publishers, **fields}


def _publisher(target: int, published: int, host: str = "127.0.0.1", protocol: str = "tcp") -> dict[str, object]:
    """Create a Compose port binding.

    Returns
    -------
    dict[str, object]
        Port binding using Docker's native status field names.
    """
    return {"TargetPort": target, "PublishedPort": published, "URL": host, "Protocol": protocol}


@pytest.fixture
def compose_status(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Capture status requests without invoking Docker.

    Returns
    -------
    Mock
        Subprocess runner returning a valid running stack by default.
    """
    output = json.dumps(
        [
            _record("grafana", [_publisher(3000, 3333)]),
            _record("prometheus"),
            _record("exporter"),
        ]
    )
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, output, ""))
    monkeypatch.setattr(status_output.subprocess, "run", runner)
    return runner


def test_summary_uses_actual_bindings_without_probing(
    tmp_path: Path, compose_status: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Selected Compose arguments and environment are retained, and only status is inspected."""
    compose = ["/usr/bin/docker", "compose", "-f", "compose.yaml", "-f", "custom.yaml"]
    private_marker = "PRIVATE_TEST_VALUE"
    environment = {"GRAFANA_PORT": "9999", "TP_LINK_PASSWORD": private_marker}
    status_output.report_startup(compose, tmp_path, environment=environment)
    output = capsys.readouterr()
    assert "Grafana: running, healthy" in output.out
    assert "Home: http://127.0.0.1:3333/" in output.out
    assert "Dashboard: http://127.0.0.1:3333/d/tp-link-power/" in output.out
    assert output.out.count("Internal service; no published HTTP port.") == 2
    assert "Docker host" in output.out
    assert "remote Docker context" in output.out
    assert "9999" not in output.out
    assert private_marker not in output.out + output.err
    compose_status.assert_called_once_with(
        [*compose, "ps", "--all", "--format", "json", "grafana", "prometheus", "exporter"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )


@pytest.mark.parametrize("json_lines", [True, False])
def test_all_service_urls_use_random_published_ports_and_configured_exporter_port(
    tmp_path: Path, compose_status: Mock, capsys: pytest.CaptureFixture[str], *, json_lines: bool
) -> None:
    """Both current JSON Lines and older array output support nondefault ports."""
    records = [
        # Fixture only; the test does not bind a socket.
        _record("grafana", [_publisher(3000, 49152, "0.0.0.0")]),  # ruff: ignore[hardcoded-bind-all-interfaces] # nosec B104
        _record("prometheus", [_publisher(9090, 49153)]),
        _record("exporter", [_publisher(8100, 49154), _publisher(8090, 9999)]),
    ]
    compose_status.return_value.stdout = (
        "\n".join(json.dumps(record) for record in records) if json_lines else json.dumps(records)
    )
    status_output.report_startup(["docker", "compose"], tmp_path, environment={"PROMETHEUS_PORT": "8100"})
    output = capsys.readouterr().out
    assert "http://127.0.0.1:49152/d/tp-link-power/" in output
    assert "http://127.0.0.1:49153/targets" in output
    assert "http://127.0.0.1:49154/metrics" in output
    assert "9999" not in output


def test_ipv6_and_duplicate_bindings(tmp_path: Path, compose_status: Mock, capsys: pytest.CaptureFixture[str]) -> None:
    """IPv6 URLs are bracketed, wildcard binds become loopback and duplicates are suppressed."""
    publishers = [
        _publisher(3000, 3333, "::"),
        _publisher(3000, 3333, "::1"),
        _publisher(3000, 4444, "2001:db8::1"),
    ]
    record = _record("grafana", publishers)
    compose_status.return_value.stdout = json.dumps([record, record])
    status_output.report_startup(["docker", "compose"], tmp_path, environment={})
    output = capsys.readouterr().out
    assert output.count("Home: http://[::1]:3333/") == 1
    assert output.count("Home: http://[2001:db8::1]:4444/") == 1
    assert "Prometheus: status unavailable" in output


@pytest.mark.parametrize(
    "publisher",
    [
        None,
        "PRIVATE_TEST_VALUE",
        _publisher(3000, 0),
        _publisher(3000, 65536),
        _publisher(3000, 3333, "user:PRIVATE_TEST_VALUE@localhost"),
        _publisher(3000, 3333, "fe80::1%PRIVATE_TEST_VALUE"),
        _publisher(3000, 3333, "127.0.0.1\nPRIVATE_TEST_VALUE"),
        _publisher(3000, 3333, protocol="udp"),
        _publisher(22, 3333),
        {"TargetPort": 3000, "PublishedPort": True, "URL": "127.0.0.1", "Protocol": "tcp"},
    ],
)
def test_invalid_bindings_never_become_urls(
    tmp_path: Path, compose_status: Mock, capsys: pytest.CaptureFixture[str], publisher: object
) -> None:
    """Only valid numeric TCP addresses and matching container ports can be printed."""
    compose_status.return_value.stdout = json.dumps([_record("grafana", [publisher])])
    status_output.report_startup(["docker", "compose"], tmp_path, environment={})
    output = capsys.readouterr()
    assert "http://" not in output.out
    assert "PRIVATE_TEST_VALUE" not in output.out + output.err


def test_unexpected_fields_are_private(
    tmp_path: Path, compose_status: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unrecognized states, arbitrary services and command/environment values are never displayed."""
    private_marker = "PRIVATE_TEST_VALUE"
    compose_status.return_value.stdout = json.dumps(
        [
            _record(
                "grafana",
                State=private_marker,
                Health=[private_marker],
                Command=private_marker,
                Environment=private_marker,
            ),
            _record(private_marker),
        ]
    )
    status_output.report_startup(["docker", "compose"], tmp_path, environment={})
    output = capsys.readouterr()
    assert "unknown state, unknown health" in output.out
    assert private_marker not in output.out + output.err


@pytest.mark.parametrize(
    "output", ["", "[]", "{}", "null", "42", '["PRIVATE_TEST_VALUE"]', "PRIVATE_TEST_VALUE", '{"Service":']
)
def test_invalid_status_is_nonfatal_and_private(
    tmp_path: Path, compose_status: Mock, capsys: pytest.CaptureFixture[str], output: str
) -> None:
    """Malformed or empty Docker responses cannot undo startup or disclose response contents."""
    compose_status.return_value.stdout = output
    status_output.report_startup(["docker", "compose"], tmp_path, environment={})
    captured = capsys.readouterr()
    assert "power-monitor status" in captured.out + captured.err
    assert "PRIVATE_TEST_VALUE" not in captured.out + captured.err


@pytest.mark.parametrize(
    "failure",
    [OSError("PRIVATE_TEST_VALUE"), subprocess.TimeoutExpired("PRIVATE_TEST_VALUE", 10, output="PRIVATE_TEST_VALUE")],
)
def test_status_subprocess_failure_is_nonfatal(
    tmp_path: Path, compose_status: Mock, capsys: pytest.CaptureFixture[str], failure: Exception
) -> None:
    """Docker access failures and timeouts show a safe fallback without overriding startup success."""
    compose_status.side_effect = failure
    status_output.report_startup(["docker", "compose"], tmp_path, environment={})
    output = capsys.readouterr()
    assert "Startup completed; service details are unavailable" in output.err
    assert "PRIVATE_TEST_VALUE" not in output.out + output.err


def test_unsuccessful_ps_output_is_not_forwarded(
    tmp_path: Path, compose_status: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither captured stream is logged when Docker rejects the status command."""
    compose_status.return_value = subprocess.CompletedProcess([], 1, "PRIVATE_TEST_VALUE", "PRIVATE_TEST_VALUE")
    status_output.report_startup(["docker", "compose"], tmp_path, environment={})
    output = capsys.readouterr()
    assert "service details are unavailable" in output.err
    assert "PRIVATE_TEST_VALUE" not in output.out + output.err


def test_service_without_healthcheck_reports_it(
    tmp_path: Path, compose_status: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Overrides that remove healthchecks must not be presented as healthy."""
    compose_status.return_value.stdout = json.dumps(_record("grafana", Health=""))
    status_output.report_startup(["docker", "compose"], tmp_path, environment={})
    assert "running, no health check" in capsys.readouterr().out
