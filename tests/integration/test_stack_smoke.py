# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exercise real monitoring services using fake metrics and disposable storage."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import TYPE_CHECKING

import pytest

from tests.integration.support import ROOT, http, mapping, wait_for
from tests.integration.test_stack_configuration import verify_published_image_device_configuration

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.integration.support import Stack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_STACK_INTEGRATION") != "1", reason="Set RUN_STACK_INTEGRATION=1 to start Docker"
    ),
]


def query(origin: str, expression: str, *, at: float | None = None) -> list[object]:
    """Evaluate a real PromQL instant query and reject parser/runtime errors.

    Returns
    -------
    list[object]
        Prometheus query result vector or scalar.
    """
    parameters = {"query": expression}
    if at is not None:
        parameters["time"] = str(at)
    result = mapping(json.loads(http(f"{origin}/api/v1/query?{urllib.parse.urlencode(parameters)}")))
    assert result["status"] == "success", result
    values = mapping(result["data"])["result"]
    assert isinstance(values, list)
    return values


def metric_values(result: list[object]) -> list[float]:
    """Extract numeric values from a Prometheus vector.

    Returns
    -------
    list[float]
        Sorted numeric sample values.
    """
    values = []
    for sample in result:
        pair = mapping(sample)["value"]
        assert isinstance(pair, list)
        values.append(float(str(pair[1])))
    return sorted(values)


def expressions(value: object) -> Iterator[str]:
    """Walk dashboard structures and yield panel PromQL expressions.

    Yields
    ------
    str
        Each panel query, including queries in collapsed rows.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "expr":
                assert isinstance(item, str)
                yield item
            else:
                yield from expressions(item)
    elif isinstance(value, list):
        for item in value:
            yield from expressions(item)


def expand(expression: str, *, host: str = ".*", alias: str = ".*") -> str:
    """Substitute Grafana variables for a deterministic integration query.

    Returns
    -------
    str
        PromQL with concrete selector and interval values.
    """
    substitutions = {
        "${job:regex}": "pyprom-exporters",
        "${exporter:regex}": "metrics:8090",
        "${host:regex}": host,
        "${device:regex}": alias,
        "$__rate_interval": "1m",
        "$__interval": "1s",
        "$__range": "1h",
    }
    for variable, value in substitutions.items():
        expression = expression.replace(variable, value)
    assert "$" not in expression, expression
    return expression


def verify_provisioning(grafana: str) -> dict[str, object]:
    """Check provisioned datasource connectivity and the deployed dashboard.

    Returns
    -------
    dict[str, object]
        Dashboard fetched from the running Grafana instance.
    """
    source = mapping(json.loads(http(f"{grafana}/api/datasources/uid/prometheus", grafana=True)))
    assert source["url"] == "http://prometheus:9090"
    assert source["type"] == "prometheus"
    health = mapping(json.loads(http(f"{grafana}/api/datasources/uid/prometheus/health", grafana=True)))
    assert health["status"] == "OK", health
    response = mapping(json.loads(http(f"{grafana}/api/dashboards/uid/tp-link-power", grafana=True)))
    dashboard = mapping(response["dashboard"])
    assert dashboard["uid"] == "tp-link-power"
    assert mapping(response["meta"])["provisioned"] is True
    assert set(expressions(dashboard)) == set(expressions(json.loads((ROOT / "dash.json").read_text())))
    return dashboard


def test_stack_provisions_queries_and_preserves_data_across_recreation(stack: Stack) -> None:
    """Validate images, live PromQL, failure visibility, and persistent storage."""
    prometheus = stack.url("prometheus", 9090)
    grafana = stack.url("grafana", 3000)
    # Execute the fixture scenarios; loading valid alert syntax alone cannot check their behavior.
    stack.compose("run", "--rm", "--no-deps", "promtool", "test", "rules", "/etc/prometheus/tests/alerts.test.yml")
    verify_published_image_device_configuration(stack)
    config_before = stack.exporter_config.read_bytes()
    assert "--no-write-config" in stack.compose("run", "--rm", "--no-deps", "exporter", "--help")
    metrics = stack.compose(
        "exec",
        "-T",
        "exporter",
        "python",
        "-c",
        "import urllib.request as r; print(r.urlopen('http://127.0.0.1:8090/metrics', timeout=5).read().decode())",
    )
    assert "tapo_discovered_devices 0.0" in metrics
    assert stack.exporter_config.read_bytes() == config_before
    wait_for(lambda: metric_values(query(prometheus, 'up{job="pyprom-exporters"}')) == [1])
    assert metric_values(query(prometheus, "current_consumption")) == [100, 250]
    assert metric_values(query(prometheus, "sum(current_consumption_today)")) == [3500]
    assert metric_values(query(prometheus, "sum(current_month_consumption)")) == [35000]
    dashboard = verify_provisioning(grafana)
    all_expressions = list(expressions(dashboard))
    assert all_expressions
    for expression in all_expressions:
        wait_for(lambda expr=expression: query(prometheus, expand(expr)))
    device_expression = next(expr for expr in all_expressions if expr.startswith("sum((current_consumption{"))
    assert metric_values(query(prometheus, expand(device_expression))) == [350]
    assert metric_values(query(prometheus, expand(device_expression, host="192[.]0[.]2[.]10"))) == [100]
    assert query(prometheus, expand(device_expression, host="does-not-exist")) == []
    assert metric_values(query(prometheus, expand(device_expression, alias=r"Desk \\(main\\)"))) == [350]
    assert query(prometheus, expand(device_expression, alias="does-not-exist")) == []
    verify_failure_visibility(stack, prometheus, device_expression)
    verify_persistence(stack, prometheus, grafana)


def verify_failure_visibility(stack: Stack, prometheus: str, device_expression: str) -> None:
    """Make exporter failure visible without presenting stale readings as live."""
    stack.compose("stop", "metrics")
    wait_for(lambda: metric_values(query(prometheus, 'up{job="pyprom-exporters"}')) == [0])
    assert query(prometheus, expand(device_expression)) == []
    stack.compose("start", "metrics")
    wait_for(lambda: metric_values(query(prometheus, 'up{job="pyprom-exporters"}')) == [1])
    assert metric_values(query(prometheus, expand(device_expression))) == [350]


def verify_persistence(stack: Stack, prometheus: str, grafana: str) -> None:
    """Recreate containers and query data written before recreation."""
    captured_at = time.time()
    before = query(prometheus, 'current_consumption{host="192.0.2.10"}', at=captured_at)
    assert metric_values(before) == [100]
    http(f"{grafana}/api/user/preferences", grafana=True, payload={"theme": "light", "timezone": "utc"})
    preferences_before = mapping(json.loads(http(f"{grafana}/api/user/preferences", grafana=True)))
    assert preferences_before["theme"] == "light"
    stack.compose("down", "--timeout", "10")
    stack.compose("up", "-d", "--wait", "--wait-timeout", "120")
    prometheus = stack.url("prometheus", 9090)
    grafana = stack.url("grafana", 3000)
    assert query(prometheus, 'current_consumption{host="192.0.2.10"}', at=captured_at) == before
    assert mapping(json.loads(http(f"{grafana}/api/user/preferences", grafana=True))) == preferences_before
    verify_provisioning(grafana)
