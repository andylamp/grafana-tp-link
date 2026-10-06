# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exercise real monitoring services using fake metrics and disposable storage."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from typing import TYPE_CHECKING

import pytest

from tests.integration.support import FIXTURES, ROOT, http, mapping, wait_for
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


def selector_pattern(selection: str | tuple[str, ...] | None) -> str:
    """Escape literal selections for RE2 and then for their enclosing PromQL string.

    Returns
    -------
    str
        Escaped regex matcher contents, with None representing Grafana's custom All value.
    """
    if selection is None:
        return ".*"
    values = (selection,) if isinstance(selection, str) else selection
    # Match the RE2 metacharacters, not Python's larger re.escape character set.
    escaped = [re.sub(r"([*+?()|\.\[\]{}^$\\])", r"\\\1", value) for value in values]
    pattern = escaped[0] if len(escaped) == 1 else "(" + "|".join(escaped) + ")"
    return json.dumps(pattern, ensure_ascii=False)[1:-1]


def expand(
    expression: str,
    *,
    host: str | tuple[str, ...] | None = None,
    alias: str | tuple[str, ...] | None = None,
    exporter: str | None = "metrics:8090",
) -> str:
    """Substitute literal selections with valid PromQL escaping for integration queries.

    Returns
    -------
    str
        PromQL with concrete selector and interval values.
    """
    substitutions = {
        "${job}": "pyprom-exporters",
        "${exporter}": selector_pattern(exporter),  # ruff: ignore[missing-f-string-syntax] - Grafana placeholder.
        "${host}": selector_pattern(host),  # ruff: ignore[missing-f-string-syntax] - Grafana placeholder.
        "${device}": selector_pattern(alias),
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


def verify_all_jobs_selection(prometheus: str, dashboard: dict[str, object]) -> None:
    """Keep the bundled Prometheus target out of All-exporter health and process readings."""
    variables = mapping(dashboard["templating"])["list"]
    assert isinstance(variables, list)
    job_variable = next(mapping(variable) for variable in variables if mapping(variable)["name"] == "job")
    available = query(prometheus, "tapo_discovered_devices")
    names = tuple(sorted({str(mapping(mapping(sample)["metric"])["job"]) for sample in available}))
    assert names == ("pyprom-exporters",)
    # Grafana uses custom All verbatim, or combines available options when it is blank.
    all_jobs = str(job_variable["allValue"]) if job_variable.get("allValue") else selector_pattern(names)
    wait_for(lambda: metric_values(query(prometheus, 'up{job="prometheus"}')) == [1])
    panel_queries = list(expressions(dashboard))
    for prefix, expected in (("count(up{", 1), ("sum(process_resident_memory_bytes{", 10485760)):
        expression = next(expression for expression in panel_queries if expression.startswith(prefix))
        selected = expand(expression.replace("${job}", all_jobs), exporter=None)
        assert metric_values(query(prometheus, selected)) == [expected]


def verify_operational_queries(prometheus: str, dashboard: dict[str, object]) -> None:
    """Exercise operational gauges, counter rates, and absence of never-successful SDK age."""
    panels = dashboard["panels"]
    assert isinstance(panels, list)
    by_title = {str(mapping(panel)["title"]): mapping(panel) for panel in panels}
    expected = {
        "Update duration": [0.12, 0.8],
        "Latest update outcome": [0, 1],
        "Failed updates per second": [0, 0],
        "Update timeouts per second": [0, 0],
        "Refresh duration": [0.9],
        "Refresh in progress": [1],
        "Scrape waits timed out per second": [0],
    }
    for title, values in expected.items():
        expression = next(expressions(by_title[title]))
        assert metric_values(query(prometheus, expand(expression))) == values
    age = next(expressions(by_title["Time since successful update"]))
    ages = query(prometheus, expand(age))
    assert len(ages) == 1
    assert mapping(mapping(ages[0])["metric"])["host"] == "192.0.2.10"
    assert metric_values(ages)[0] > 0
    assert query(prometheus, expand(age, host="192.0.2.11")) == []
    state = next(expressions(by_title["Latest update outcome"]))
    assert metric_values(query(prometheus, expand(state, host="192.0.2.11"))) == [0]


def verify_published_image_producer(stack: Stack) -> None:
    """Run real producer assertions inside the image without consulting physical devices."""
    script = (FIXTURES / "producer_probe.py").read_text(encoding="utf-8")
    report = mapping(json.loads(stack.compose("exec", "-T", "exporter", "python", "-", input_text=script)))
    assert report["producer_verified"] is True
    assert report["pending_tasks"] == 0
    assert isinstance(report["version"], str)


def test_stack_provisions_queries_and_preserves_data_across_recreation(stack: Stack) -> None:
    """Validate images, live PromQL, failure visibility, and persistent storage."""
    prometheus = stack.url("prometheus", 9090)
    grafana = stack.url("grafana", 3000)
    # Execute the fixture scenarios; loading valid alert syntax alone cannot check their behavior.
    stack.compose("run", "--rm", "--no-deps", "promtool", "test", "rules", "/etc/prometheus/tests/alerts.test.yml")
    verify_published_image_device_configuration(stack)
    verify_published_image_producer(stack)
    config_before = stack.exporter_config.read_bytes()
    assert "--no-write-config" in stack.compose("run", "--rm", "--no-deps", "exporter", "--help")
    metrics = stack.compose(
        "exec",
        "-T",
        "exporter",
        "python",
        "-c",
        f"import urllib.request as r; print(r.urlopen('http://127.0.0.1:{stack.exporter_port}/metrics',timeout=5).read().decode())",
    )
    assert "tapo_discovered_devices 0.0" in metrics
    assert stack.exporter_config.read_bytes() == config_before
    wait_for(lambda: metric_values(query(prometheus, 'up{job="pyprom-exporters"}')) == [1])
    assert metric_values(query(prometheus, "current_consumption")) == [100, 250]
    assert metric_values(query(prometheus, "sum(current_consumption_today)")) == [3500]
    assert metric_values(query(prometheus, "sum(current_month_consumption)")) == [35000]
    dashboard = verify_provisioning(grafana)
    verify_all_jobs_selection(prometheus, dashboard)
    all_expressions = list(expressions(dashboard))
    assert all_expressions
    for expression in all_expressions:
        wait_for(lambda expr=expression: query(prometheus, expand(expr)))
    verify_operational_queries(prometheus, dashboard)
    device_expression = next(expr for expr in all_expressions if expr.startswith("sum((current_consumption{"))
    assert metric_values(query(prometheus, expand(device_expression))) == [350]
    assert metric_values(query(prometheus, expand(device_expression, host="192.0.2.10"))) == [100]
    assert query(prometheus, expand(device_expression, host="does-not-exist")) == []
    assert metric_values(query(prometheus, expand(device_expression, alias="Desk (main)"))) == [350]
    assert query(prometheus, expand(device_expression, alias="does-not-exist")) == []
    assert metric_values(query(prometheus, expand(device_expression, host=("192.0.2.10", "192.0.2.11")))) == [350]
    assert query(prometheus, expand(device_expression, host="192.0.2.1.")) == []
    assert query(prometheus, expand(device_expression, alias="Desk .*")) == []
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
