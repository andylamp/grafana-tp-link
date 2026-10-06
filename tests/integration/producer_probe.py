# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Verify the installed exporter inside its image using only simulated device I/O.

This script runs through ``python -`` in the exporter container. Its imports are
provided by that image, not by the stack utility's development environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sys
from dataclasses import dataclass, field
from importlib.metadata import version
from unittest.mock import patch

from kasa import Credentials  # ty: ignore[unresolved-import] - Installed in the exporter image.
from pyprom_exporters.benchmarks.scalability import (  # ty: ignore[unresolved-import] - Image-owned simulation helpers.
    Activity,
    SimulatedDevice,
)
from pyprom_exporters.exporters.tapo import (  # ty: ignore[unresolved-import] - Exercise the installed producer itself.
    TapoDiscoveryOptions,
    TapoExporterOptions,
    TapoPowerPlugPrometheusExporter,
    TapoPrometheusOptions,
)

HEALTHY_HOST = "192.0.2.10"
BLOCKED_HOST = "192.0.2.11"


@dataclass
class BlockedDevice(SimulatedDevice):
    """Wait forever during updates while allowing deterministic cancellation and closure."""

    host: str
    activity: Activity
    alias: str = "blocked"
    blocked: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    closed: bool = False

    async def update(self) -> None:
        """Require the exporter's whole-update deadline to interrupt this simulated device."""
        try:
            await self.blocked.wait()
        finally:
            self.cancelled = True

    async def disconnect(self) -> None:
        """Record session closure without creating a socket."""
        self.closed = True


def verify_samples(exporter: TapoPowerPlugPrometheusExporter) -> None:
    """Assert independent power readings and every operational metric from the real producer."""
    samples = {
        (sample.name, sample.labels.get("host", "")): sample.value
        for metric in exporter.collect()
        for sample in metric.samples
    }
    assert samples["current_consumption", HEALTHY_HOST] == 5
    assert ("current_consumption", BLOCKED_HOST) not in samples
    for host, success in ((HEALTHY_HOST, 1), (BLOCKED_HOST, 0)):
        assert samples["tapo_device_update_success", host] == success
        timestamp = samples["tapo_device_last_success_timestamp_seconds", host]
        assert math.isfinite(timestamp)
        assert (timestamp > 0) if success else (timestamp == 0)
        duration = samples["tapo_device_update_duration_seconds", host]
        assert math.isfinite(duration)
        assert duration > 0
        for name in ("tapo_device_update_failures_total", "tapo_device_update_timeouts_total"):
            assert samples[name, host] == 1 - success
    duration = samples["tapo_refresh_duration_seconds", ""]
    assert math.isfinite(duration)
    assert duration > 0
    assert samples["tapo_refresh_in_progress", ""] == 0
    assert samples["tapo_scrape_refresh_timeouts_total", ""] == 0


async def verify_producer() -> dict[str, object]:
    """Run one bounded pass and prove cleanup leaves no background work.

    Returns
    -------
    dict[str, object]
        Credential-free evidence of the installed version and completed assertions.
    """
    installed = version("pyprom-exporters")
    release = re.match(r"^(\d+)\.(\d+)\.(\d+)", installed)
    assert release is not None, installed
    assert tuple(map(int, release.groups())) >= (0, 3, 0), installed
    original_tasks = asyncio.all_tasks()
    activity = Activity(latency=0)
    healthy = SimulatedDevice(host=HEALTHY_HOST, activity=activity, alias="healthy")
    blocked = BlockedDevice(host=BLOCKED_HOST, activity=activity, alias="blocked")
    exporter = TapoPowerPlugPrometheusExporter(
        asyncio.get_running_loop(),
        TapoExporterOptions(
            devices=[],
            max_concurrent_devices=2,
            update_timeout=0.05,
            discovery_options=TapoDiscoveryOptions(perform_discovery=False, credentials=Credentials()),
            prometheus_options=TapoPrometheusOptions(refresh_interval=None),
        ),
    )
    exporter.discovered_devices = {HEALTHY_HOST: healthy, BLOCKED_HOST: blocked}
    # Even accidental recovery must fail locally instead of probing any device address.
    with (
        patch("pyprom_exporters.exporters.tapo.Discover.discover", side_effect=AssertionError("Discovery forbidden")),
        patch(
            "pyprom_exporters.exporters.tapo.Discover.discover_single",
            side_effect=AssertionError("Discovery forbidden"),
        ),
    ):
        try:
            await asyncio.wait_for(exporter.update_and_collect(), timeout=2)
            verify_samples(exporter)
            assert blocked.cancelled
            assert blocked.closed
        finally:
            await asyncio.wait_for(exporter.cleanup(), timeout=2)
    assert not (asyncio.all_tasks() - original_tasks)
    assert activity.active == 0
    assert exporter.discovered_devices == {}
    assert not exporter._pending_disconnects
    return {"version": installed, "producer_verified": True, "pending_tasks": 0}


if __name__ == "__main__":
    # The timed-out fixture is expected; keep the successful JSON report free of warning logs.
    logging.disable(logging.CRITICAL)
    sys.stdout.write(json.dumps(asyncio.run(verify_producer())) + "\n")
