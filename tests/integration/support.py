# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Run a disposable monitoring stack without accessing device networks."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - Explicit Docker argv without a shell.
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import yaml

from grafana_tp_link.configuration import load_configuration, render_configuration

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent
TEST_PASSWORD = "isolated-test-password"  # ruff: ignore[hardcoded-password-string] - Disposable test credential.


def mapping(value: object) -> dict[str, object]:
    """Require a JSON/YAML object and preserve explicit types.

    Returns
    -------
    dict[str, object]
        Validated object with string keys.
    """
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast("dict[str, object]", value)


def run(arguments: list[str], *, environment: dict[str, str] | None = None, input_text: str | None = None) -> str:
    """Run a bounded command and retain output for useful failure reports.

    Returns
    -------
    str
        Combined command output.
    """
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - Explicit argv and resolved Docker path.
        arguments,
        env=environment,
        input=input_text,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout
    return result.stdout


def http(url: str, *, grafana: bool = False, payload: dict[str, object] | None = None) -> str:
    """Read an isolated loopback endpoint with a bounded request timeout.

    Returns
    -------
    str
        UTF-8 response body.
    """
    parsed = urllib.parse.urlsplit(url)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.scheme == "http"
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(  # ruff: ignore[suspicious-url-open-usage] - Loopback HTTP asserted above.
        url, data=body, method="PUT" if payload is not None else "GET"
    )
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if grafana:
        token = base64.b64encode(f"admin:{TEST_PASSWORD}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=5) as response:  # ruff: ignore[suspicious-url-open-usage] - Loopback HTTP asserted above.
        return response.read().decode()


def wait_for(probe: Callable[[], object], *, timeout: float = 90) -> object:
    """Poll a bounded readiness or consistency condition.

    Returns
    -------
    object
        The first truthy result from the probe.
    """
    deadline = time.monotonic() + timeout
    last_failure: object = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
            last_failure = result
        except (OSError, urllib.error.URLError, AssertionError, ValueError) as exc:
            last_failure = str(exc)
        time.sleep(0.5)
    pytest.fail(f"Condition did not become ready within {timeout}s: {last_failure}")


@dataclass
class Stack:
    """Own the identity and resources of one disposable Compose project."""

    docker: str
    project: str
    compose_file: Path
    environment: dict[str, str] = field(repr=False)
    exporter_config: Path
    directory: Path

    @property
    def exporter_port(self) -> int:
        """The rendered exporter HTTP port."""
        port = mapping(yaml.safe_load(self.exporter_config.read_text()))["prometheus_port"]
        assert isinstance(port, int)
        return port

    def compose(self, *arguments: str, input_text: str | None = None) -> str:
        """Run Compose against only this test's generated project.

        Returns
        -------
        str
            Combined command output.
        """
        return run(
            [self.docker, "compose", "-p", self.project, "-f", str(self.compose_file), *arguments],
            environment=self.environment,
            input_text=input_text,
        )

    def url(self, service: str, port: int) -> str:
        """Find Docker's randomly allocated loopback port.

        Returns
        -------
        str
            HTTP origin for a published service.
        """
        assert service in {"grafana", "prometheus"}
        endpoint = self.compose("port", service, str(port)).strip()
        assert endpoint.startswith("127.0.0.1:")
        return f"http://{endpoint}"


def isolated_exporter_configuration(source: str) -> str:
    """Clear device targets and credentials while retaining unrelated exporter settings.

    Returns
    -------
    str
        A YAML copy suitable for running the image without device discovery.
    """
    config = mapping(yaml.safe_load(source))
    exporter = mapping(mapping(config["exporters"])["tapo"])
    exporter["devices"] = []
    discovery = mapping(exporter.setdefault("discovery_options", {}))
    discovery["perform_discovery"] = False
    discovery.pop("credentials", None)
    return yaml.safe_dump(config, sort_keys=False)


def prepare_checkout(directory: Path) -> dict[str, object]:
    """Copy stack templates and sanitize user settings into a disposable checkout.

    Returns
    -------
    dict[str, object]
        Canonical configuration with device I/O and credentials removed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("prometheus", "grafana"):
        shutil.copytree(ROOT / name, directory / name)
    for name in ("compose.yaml", "dash.json"):
        shutil.copyfile(ROOT / name, directory / name)
    (directory / "config").mkdir()
    config = mapping(yaml.safe_load((ROOT / "config/stack.yaml").read_text()))
    native = yaml.safe_dump(config["exporter"])
    config["exporter"] = mapping(yaml.safe_load(isolated_exporter_configuration(native)))
    (directory / "config/stack.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    return config


def create_stack(tmp_path: Path) -> Stack:
    """Derive a test project from the real Compose configuration.

    Returns
    -------
    Stack
        Isolated project whose lifecycle has not yet started.
    """
    docker = shutil.which("docker")
    assert docker is not None, "Docker is required when integration tests are explicitly enabled"
    project = f"pyprom-test-{uuid.uuid4().hex[:12]}"
    configuration = prepare_checkout(tmp_path)
    native = mapping(configuration["exporter"])
    # Nondefault settings expose stale image/environment defaults in the real runtime.
    native.update({"prometheus_port": 18090, "log_level": "WARNING"})
    (tmp_path / "config/stack.yaml").write_text(yaml.safe_dump(configuration, sort_keys=False))
    configuration = load_configuration(tmp_path, {}, require_devices=False)
    runtime = render_configuration(tmp_path, configuration)
    prometheus_options = mapping(configuration["prometheus"])
    environment = dict(os.environ)
    environment.update(
        {
            "GRAFANA_ADMIN_USER": "admin",
            "GRAFANA_ADMIN_PASSWORD": TEST_PASSWORD,
            "TP_LINK_USERNAME": "",
            "TP_LINK_PASSWORD": "",
            "TAPO_PLUG_DEVICES": "",
            "GRAFANA_BIND_ADDRESS": "127.0.0.1",
            "GRAFANA_PORT": "3000",
            "PROMETHEUS_PORT": str(native["prometheus_port"]),
            "PYPROM_EXPORTERS_LOG_LEVEL": str(native["log_level"]),
            "PROMETHEUS_RETENTION_TIME": str(prometheus_options["retention_time"]),
            "PROMETHEUS_RETENTION_SIZE": str(prometheus_options["retention_size"]),
            "PYPROM_RUNTIME_DIR": str(runtime),
        }
    )
    env_file = tmp_path / "empty.env"
    env_file.touch()
    model = mapping(
        json.loads(
            run(
                [
                    docker,
                    "compose",
                    "--env-file",
                    str(env_file),
                    "-p",
                    project,
                    "-f",
                    str(tmp_path / "compose.yaml"),
                    "config",
                    "--format",
                    "json",
                ],
                environment=environment,
            )
        )
    )
    services = mapping(model["services"])
    exporter = mapping(services["exporter"])
    configure_fake_metrics(services, exporter["image"], tmp_path, runtime)
    services["promtool"] = {
        "image": mapping(services["prometheus"])["image"],
        "entrypoint": ["/bin/promtool"],
        "profiles": ["test-tools"],
        "network_mode": "none",
        "read_only": True,
        "tmpfs": mapping(services["prometheus"])["tmpfs"],
        "volumes": [
            {"type": "bind", "source": str(tmp_path / "prometheus"), "target": "/etc/prometheus", "read_only": True}
        ],
    }
    for name in ("grafana", "prometheus", "exporter"):
        service = mapping(services[name])
        service.pop("container_name", None)
        service.pop("ports", None)
        service["restart"] = "no"
    # Only browser/query services receive host access. Both exporters have no LAN route.
    for name, port in (("grafana", 3000), ("prometheus", 9090)):
        service = mapping(services[name])
        service["networks"] = {"default": None, "gateway": None}
        service["ports"] = [{"target": port, "published": "0", "host_ip": "127.0.0.1", "protocol": "tcp"}]
    model["networks"] = {"default": {"internal": True}, "gateway": {}}
    for volume in mapping(model.get("volumes", {})).values():
        volume_config = mapping(volume)
        assert not volume_config.get("external")
        assert str(volume_config.get("name", "")).startswith(project)
    compose_file = tmp_path / "compose.json"
    compose_file.write_text(json.dumps(model))
    return Stack(docker, project, compose_file, environment, runtime / "exporter.yaml", tmp_path)


def configure_fake_metrics(services: dict[str, object], image: object, tmp_path: Path, runtime: Path) -> None:
    """Replace only scrape targets and timing while retaining production rules."""
    config = mapping(yaml.safe_load((runtime / "prometheus.yml").read_text()))
    global_config = mapping(config.setdefault("global", {}))
    global_config.update({"scrape_interval": "1s", "scrape_timeout": "800ms", "evaluation_interval": "1s"})
    jobs = config["scrape_configs"]
    assert isinstance(jobs, list)
    for job in jobs:
        scrape = mapping(job)
        scrape["scrape_interval"] = "1s"
        scrape["scrape_timeout"] = "800ms"
        if scrape["job_name"] == "pyprom-exporters":
            scrape["static_configs"] = [{"targets": ["metrics:8090"]}]
    config_file = tmp_path / "fake-prometheus.yml"
    config_file.write_text(yaml.safe_dump(config))
    prometheus = mapping(services["prometheus"])
    volumes = prometheus["volumes"]
    assert isinstance(volumes, list)
    for volume in volumes:
        mount = mapping(volume)
        if mount["target"] == "/etc/prometheus/prometheus.yml":
            mount["source"] = str(config_file)
    services["metrics"] = {
        "image": image,
        "entrypoint": ["python", "/fixtures/metrics_server.py"],
        "volumes": [{"type": "bind", "source": str(FIXTURES), "target": "/fixtures", "read_only": True}],
        "networks": {"default": None},
    }


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    """Start and always remove only this fixture's disposable containers and volumes.

    Yields
    ------
    Stack
        Running isolated test project.
    """
    instance = create_stack(tmp_path)
    try:
        instance.compose("up", "-d", "--wait", "--wait-timeout", "120")
        yield instance
    finally:
        instance.compose("down", "--volumes", "--remove-orphans", "--timeout", "10")
