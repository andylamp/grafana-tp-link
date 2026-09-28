# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Validate destructive reset only against disposable integration resources."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.integration.support import mapping, run

if TYPE_CHECKING:
    from tests.integration.support import Stack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_STACK_INTEGRATION") != "1", reason="Set RUN_STACK_INTEGRATION=1 to start Docker"
    ),
]


def project_resources(stack: Stack) -> dict[str, set[str]]:
    """List only resources bearing this fixture's unique Compose project label.

    Returns
    -------
    dict[str, set[str]]
        Container, network, and volume identifiers belonging to the test project.
    """
    selector = f"label=com.docker.compose.project={stack.project}"
    return {
        "containers": set(
            run([stack.docker, "ps", "--all", "--filter", selector, "--format", "{{.ID}}"]).splitlines()
        ),
        "networks": set(
            run([stack.docker, "network", "ls", "--filter", selector, "--format", "{{.ID}}"]).splitlines()
        ),
        "volumes": set(
            run([stack.docker, "volume", "ls", "--filter", selector, "--format", "{{.Name}}"]).splitlines()
        ),
    }


def image_ids(stack: Stack, model: dict[str, object]) -> dict[str, str]:
    """Record the pinned image identities without changing the local image store.

    Returns
    -------
    dict[str, str]
        Image reference to Docker image identifier.
    """
    images = {str(mapping(service)["image"]) for service in mapping(model["services"]).values()}
    return {image: run([stack.docker, "image", "inspect", "--format", "{{.Id}}", image]).strip() for image in images}


@pytest.mark.parametrize("environment_exists", [True, False], ids=["with-env", "without-env"])
def test_reset_removes_only_selected_project_data(stack: Stack, tmp_path: Path, *, environment_exists: bool) -> None:
    """Reset deletes owned Docker resources while preserving files, images, and unrelated storage."""
    checkout = tmp_path / "reset-checkout"
    checkout.mkdir()
    model = mapping(json.loads(stack.compose_file.read_text()))
    assert model["name"] == stack.project
    assert stack.compose("ps", "--quiet", "metrics").strip()
    # Keep the running fixture container as a real orphan in this copied definition.
    mapping(model["services"]).pop("metrics")
    compose_file = checkout / "compose.yaml"
    compose_file.write_text(json.dumps(model))
    (checkout / "config").mkdir()
    config_file = checkout / "config/stack.yaml"
    config_file.write_bytes((stack.directory / "config/stack.yaml").read_bytes())
    env_file = checkout / ".env"
    if environment_exists:
        env_file.write_text(f"COMPOSE_PROJECT_NAME={stack.project}\n# Existing settings must survive reset.\n")
        env_file.chmod(0o600)
    retained_files = {
        path: (path.read_bytes(), path.stat().st_mode)
        for path in (compose_file, config_file, stack.exporter_config, env_file)
        if path.exists()
    }
    resources_before = project_resources(stack)
    assert all(resources_before.values()), resources_before
    images_before = image_ids(stack, model)
    unrelated_volume = f"{stack.project}-unrelated-storage"
    run([stack.docker, "volume", "create", unrelated_volume])
    try:
        # Override ambient project settings as well as checking the generated project name.
        environment = stack.environment | {"COMPOSE_PROJECT_NAME": stack.project}
        run(
            [str(Path(sys.executable).with_name("power-monitor")), "--directory", str(checkout), "reset", "--yes"],
            environment=environment,
        )
        resources_after = project_resources(stack)
        assert not any(resources_after.values()), resources_after
        for path, expected in retained_files.items():
            assert (path.read_bytes(), path.stat().st_mode) == expected
        assert env_file.exists() is environment_exists
        assert image_ids(stack, model) == images_before
        assert (
            run([stack.docker, "volume", "inspect", "--format", "{{.Name}}", unrelated_volume]).strip()
            == unrelated_volume
        )
    finally:
        volumes = run([stack.docker, "volume", "ls", "--format", "{{.Name}}"]).splitlines()
        if unrelated_volume in volumes:
            run([stack.docker, "volume", "rm", unrelated_volume])
