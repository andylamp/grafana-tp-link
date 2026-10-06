# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exercise explicit configuration and host storage using only disposable paths."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from tests.integration.support import http, mapping, run, wait_for
from tests.integration.test_stack_configuration import (
    cli,
    prepare_cli_stack,
    runtime_mount,
    verify_rendered_configuration,
    write_environment,
)
from tests.integration.test_stack_reset import project_resources
from tests.integration.test_stack_smoke import metric_values, query

if TYPE_CHECKING:
    from tests.integration.support import Stack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_STACK_INTEGRATION") != "1", reason="Set RUN_STACK_INTEGRATION=1 to start Docker"
    ),
]


def prepare_selected_storage(instance: Stack, tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """Select external YAML with relative and absolute disposable bind directories.

    Returns
    -------
    tuple[Path, dict[str, Path]]
        Selected YAML file outside the checkout and its service data directories.
    """
    default = instance.directory / "config/stack.yaml"
    config = mapping(yaml.safe_load(default.read_text()))
    directories = {name: tmp_path / "owned data $cache" / name for name in ("grafana", "prometheus")}
    for directory in directories.values():
        directory.mkdir(parents=True)
        (directory / "keep.txt").write_text("Owned fixture data must survive reset.\n")
    mapping(config["grafana"])["data_directory"] = "../owned data $cache/grafana"
    mapping(config["prometheus"]).update(
        {
            "data_directory": str(directories["prometheus"]),
            "scrape_interval": "1s",
            "scrape_timeout": "800ms",
            "evaluation_interval": "1s",
        }
    )
    native = mapping(config["exporter"])
    native.update({"prometheus_port": 18094, "log_level": "DEBUG"})
    tapo = mapping(mapping(native["exporters"])["tapo"])
    mapping(tapo["prometheus_options"])["scrape_timeout"] = 0.5
    selected = tmp_path / "selected stack.yaml"
    selected.write_text(yaml.safe_dump(config, sort_keys=False))
    # A selected file must stand alone; consulting the default would fail validation.
    default.write_text("# This default must not be read or merged.\ninvalid_default: true\n")
    compose = instance.directory / "compose.yaml"
    model = mapping(yaml.safe_load(compose.read_text()))
    for name in directories:
        # Own the disposable database files without requiring a root cleanup container.
        mapping(mapping(model["services"])[name])["user"] = f"{os.getuid()}:{os.getgid()}"
    compose.write_text(yaml.safe_dump(model, sort_keys=False))
    return selected, directories


def verify_bind_mounts(instance: Stack, directories: dict[str, Path]) -> None:
    """Verify Docker used the literal selected paths for both writable data mounts."""
    for name, directory in directories.items():
        container = instance.compose("ps", "--quiet", name).strip()
        mounts = json.loads(run([instance.docker, "inspect", "--format", "{{json .Mounts}}", container]))
        target = "/var/lib/grafana" if name == "grafana" else "/prometheus"
        mount = next(mapping(value) for value in mounts if mapping(value)["Destination"] == target)
        assert mount["Type"] == "bind"
        assert mount["Source"] == str(directory)
        assert mount["RW"] is True


def test_selected_yaml_bind_storage_persists_and_reset_preserves_host_files(tmp_path: Path) -> None:
    """Real CLI selection and recreation preserve bind data, including literal dollar signs."""
    instance, environment = prepare_cli_stack(tmp_path / "checkout")
    selected, directories = prepare_selected_storage(instance, tmp_path)
    env_content = write_environment(instance.directory)
    source_bytes = selected.read_bytes()
    try:
        cli(instance, environment, "check", config=Path("../selected stack.yaml"))
        cli(instance, environment, "up", config=selected)
        verify_rendered_configuration(runtime_mount(instance), port=18094, level="DEBUG")
        verify_bind_mounts(instance, directories)
        assert not (directories["grafana"] / "dashboards").exists()
        prometheus = instance.url("prometheus", 9090)
        wait_for(lambda: metric_values(query(prometheus, 'current_consumption{host="192.0.2.10"}')) == [100])
        captured_at = time.time()
        before = query(prometheus, 'current_consumption{host="192.0.2.10"}', at=captured_at)
        grafana = instance.url("grafana", 3000)
        http(f"{grafana}/api/user/preferences", grafana=True, payload={"theme": "light", "timezone": "utc"})
        cli(instance, environment, "down", config=selected)
        cli(instance, environment, "up", config=selected)
        verify_bind_mounts(instance, directories)
        assert not (directories["grafana"] / "dashboards").exists()
        prometheus = instance.url("prometheus", 9090)
        assert query(prometheus, 'current_consumption{host="192.0.2.10"}', at=captured_at) == before
        grafana = instance.url("grafana", 3000)
        assert mapping(json.loads(http(f"{grafana}/api/user/preferences", grafana=True)))["theme"] == "light"
        cli(instance, environment, "reset", "--yes", config=selected)
        assert not any(project_resources(instance).values())
        for directory in directories.values():
            assert directory.is_dir()
            assert (directory / "keep.txt").read_text() == "Owned fixture data must survive reset.\n"
        assert (directories["grafana"] / "grafana.db").is_file()
        assert not (directories["grafana"] / "dashboards").exists()
        assert (directories["prometheus"] / "wal").is_dir()
        assert selected.read_bytes() == source_bytes
        assert (instance.directory / ".env").read_text() == env_content
    finally:
        instance.compose("down", "--volumes", "--remove-orphans", "--timeout", "10")


@pytest.mark.parametrize("command", ["check", "up"])
def test_missing_bind_directory_is_rejected_without_creation(tmp_path: Path, command: str) -> None:
    """Docker must reject a missing bind source without silently creating a directory."""
    instance, environment = prepare_cli_stack(tmp_path)
    write_environment(tmp_path)
    missing = tmp_path / "missing data $directory"
    config_file = tmp_path / "config/stack.yaml"
    config = mapping(yaml.safe_load(config_file.read_text()))
    mapping(config["prometheus"])["data_directory"] = str(missing)
    config_file.write_text(yaml.safe_dump(config, sort_keys=False))
    original = config_file.read_bytes()
    try:
        with pytest.raises(AssertionError, match="bind source path does not exist"):
            cli(instance, environment, command)
        assert not missing.exists()
        assert config_file.read_bytes() == original
    finally:
        instance.compose("down", "--volumes", "--remove-orphans", "--timeout", "10")
    assert not any(project_resources(instance).values())
