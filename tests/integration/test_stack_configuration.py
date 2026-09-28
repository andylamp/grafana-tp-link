# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Verify native device configuration using the published image and real CLI."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.integration.support import ROOT, create_stack, mapping, run

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
    print(json.dumps({"results": results, "unchanged": path.read_bytes() == original}))
"""


def verify_published_image_device_configuration(stack: Stack) -> None:
    """Exercise pure configuration loading without creating or querying any device."""
    output = stack.compose("exec", "-T", "exporter", "python", "-c", CONFIG_LOADER, input_text=COMMENTED_CONFIG)
    assert json.loads(output) == {
        "results": [
            {"exists": True, "devices": ["192.0.2.10", "192.0.2.11"]},
            {"exists": True, "devices": ["192.0.2.10", "192.0.2.11"]},
            {"exists": True, "devices": ["192.0.2.20", "192.0.2.21"]},
        ],
        "unchanged": True,
    }


def test_check_accepts_commented_yaml_devices_without_starting_exporter(tmp_path: Path) -> None:
    """Check validates YAML hosts with blank environment overrides and starts only promtool."""
    instance = create_stack(tmp_path)
    checkout = tmp_path / "check-checkout"
    (checkout / "config").mkdir(parents=True)
    config_file = checkout / "config/exporter.yaml"
    config_file.write_text(COMMENTED_CONFIG)
    env_file = checkout / ".env"
    env_file.write_text("TAPO_PLUG_DEVICES=\n")
    env_file.chmod(0o600)
    shutil.copytree(ROOT / "prometheus", checkout / "prometheus")
    model = mapping(json.loads(instance.compose_file.read_text()))
    assert model["name"] == instance.project
    services = mapping(model["services"])
    mounts = mapping(services["exporter"])["volumes"]
    assert isinstance(mounts, list)
    for volume in mounts:
        mount = mapping(volume)
        if mount["target"] == "/etc/pyprom-exporters/config.yaml":
            mount["source"] = str(config_file)
    prometheus = mapping(services["prometheus"])
    prometheus.pop("networks", None)
    prometheus["network_mode"] = "none"
    (checkout / "compose.yaml").write_text(json.dumps(model))
    environment = instance.environment | {"COMPOSE_PROJECT_NAME": instance.project, "TAPO_PLUG_DEVICES": ""}
    started_at = str(int(time.time()) - 1)
    try:
        run(
            [str(Path(sys.executable).with_name("power-monitor")), "--directory", str(checkout), "check"],
            environment=environment,
        )
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
        assert config_file.read_text() == COMMENTED_CONFIG
        assert env_file.read_text() == "TAPO_PLUG_DEVICES=\n"
    finally:
        instance.compose("down", "--volumes", "--remove-orphans", "--timeout", "10")
