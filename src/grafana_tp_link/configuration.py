# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Validate unified settings and publish immutable, credential-free runtime files."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import math
import os
import re
import tempfile
from pathlib import Path
from typing import cast

import yaml
from yaml.resolver import BaseResolver

OVERRIDE_PATHS = {
    "GRAFANA_BIND_ADDRESS": ("grafana", "bind_address"),
    "GRAFANA_PORT": ("grafana", "port"),
    "PROMETHEUS_RETENTION_TIME": ("prometheus", "retention_time"),
    "PROMETHEUS_RETENTION_SIZE": ("prometheus", "retention_size"),
    "PROMETHEUS_SCRAPE_INTERVAL": ("prometheus", "scrape_interval"),
    "PROMETHEUS_SCRAPE_TIMEOUT": ("prometheus", "scrape_timeout"),
    "PROMETHEUS_EVALUATION_INTERVAL": ("prometheus", "evaluation_interval"),
    "PROMETHEUS_PORT": ("exporter", "prometheus_port"),
    "PYPROM_EXPORTERS_LOG_LEVEL": ("exporter", "log_level"),
    "TAPO_PLUG_DEVICES": ("exporter", "exporters", "tapo", "devices"),
}
OVERRIDE_NAMES = tuple(OVERRIDE_PATHS)
GRAFANA_KEYS = {"bind_address", "port"}
PROMETHEUS_KEYS = {"retention_time", "retention_size", "scrape_interval", "scrape_timeout", "evaluation_interval"}
CREDENTIAL_KEYS = {
    "credentials",
    "username",
    "password",
    "admin_user",
    "admin_password",
    "tapo_username",
    "tapo_password",
    "tp_link_username",
    "tp_link_password",
    "api_key",
    "token",
    "bearer_token",
    "secret",
    "secret_key",
    "securejsondata",
}
PORT_OVERRIDES = {"GRAFANA_PORT", "PROMETHEUS_PORT"}
LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET", "WARN", "FATAL"}
MAX_PORT = 65535
MAX_DURATION_MILLISECONDS = ((1 << 63) - 1) // 1_000_000
DURATION_MULTIPLIERS = (31_536_000_000, 604_800_000, 86_400_000, 3_600_000, 60_000, 1000, 1)
DURATION_PATTERN = re.compile(r"(?:(\d+)y)?(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?(?:(\d+)ms)?")
SIZE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)(B|KB|MB|GB|TB|PB|EB)")
SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB", "EB")
HOST_LABEL = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9_])?")
MAX_HOST_LENGTH = 253
MAX_LABEL_LENGTH = 63
MAX_DURATION_LENGTH = 128
MAX_SIZE_LENGTH = 128


class _UniqueKeyLoader(yaml.SafeLoader):
    """Keep safe YAML constructors while rejecting repeated explicit mapping keys."""


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[str, object]:
    """Reject duplicate explicit keys before standard YAML merge processing.

    Returns
    -------
    dict[str, object]
        Safely constructed mapping, including supported merge-key defaults.

    Raises
    ------
    yaml.YAMLError
        If explicit mapping keys are repeated or are not strings.
    """
    seen = set()
    for key_node, _value_node in node.value:
        key = "<<" if key_node.tag == "tag:yaml.org,2002:merge" else loader.construct_object(key_node)
        if not isinstance(key, str) or key in seen:
            message = "YAML mapping keys must be unique strings."
            raise yaml.YAMLError(message)
        seen.add(key)
    return cast("dict[str, object]", loader.construct_mapping(node))


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _mapping(value: object, location: str) -> dict[str, object]:
    """Require a string-keyed mapping without including its contents in errors.

    Returns
    -------
    dict[str, object]
        The checked mapping.

    Raises
    ------
    ValueError
        If the value is not a string-keyed mapping.
    """
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        message = f"{location} must be a mapping with string keys."
        raise ValueError(message)
    return cast("dict[str, object]", value)


def _parse_yaml(text: str) -> object:
    """Parse safe YAML and dispose parser state on success or failure.

    Returns
    -------
    object
        The parsed document, before stack-shape validation.
    """
    loader = _UniqueKeyLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _read_mapping(path: Path, location: str) -> dict[str, object]:
    """Load YAML without exposing source lines in parser diagnostics.

    Returns
    -------
    dict[str, object]
        The loaded mapping.

    Raises
    ------
    ValueError
        If YAML, encoding, or the document shape is invalid.
    """
    try:
        value = _parse_yaml(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeError) as error:
        message = f"Invalid {location}; check YAML syntax and UTF-8 encoding."
        raise ValueError(message) from error
    return _mapping(value, location)


def _validate_tree(value: object, ancestors: set[int] | None = None) -> None:
    """Reject credential fields, recursive aliases and nonportable YAML values.

    Raises
    ------
    ValueError
        If a credential field, cycle, or unsupported value appears.
    """
    if ancestors is None:
        ancestors = set()
    if isinstance(value, (dict, list)):
        if id(value) in ancestors:
            message = "Recursive YAML aliases are not supported."
            raise ValueError(message)
        ancestors.add(id(value))
        if isinstance(value, dict):
            values = _mapping(value, "Configuration")
            if CREDENTIAL_KEYS.intersection(key.casefold() for key in values):
                message = "Credentials must be supplied through the environment, not configuration YAML."
                raise ValueError(message)
            children = values.values()
        else:
            children = value
        for child in children:
            _validate_tree(child, ancestors)
        ancestors.remove(id(value))
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        message = "Configuration YAML supports only mappings, lists and scalar settings."
        raise ValueError(message)
    elif isinstance(value, float) and not math.isfinite(value):
        message = "Numeric settings must be finite."
        raise ValueError(message)


def _require_keys(options: dict[str, object], keys: set[str], location: str) -> None:
    """Reject missing or unknown stack settings without echoing their values.

    Raises
    ------
    ValueError
        If settings differ from the supported keys.
    """
    if options.keys() != keys:
        message = f"{location} must contain exactly these settings: {', '.join(sorted(keys))}."
        raise ValueError(message)


def _native_sections(
    configuration: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Apply native defaults needed to validate the stack's shared settings.

    Returns
    -------
    tuple[dict[str, object], dict[str, object], dict[str, object]]
        Exporter, Tapo, and Prometheus-option mappings.
    """
    exporter = _mapping(configuration.get("exporter"), "exporter")
    exporter.setdefault("prometheus_port", 8090)
    exporter.setdefault("log_level", "INFO")
    exporters = _mapping(exporter.setdefault("exporters", {}), "exporter.exporters")
    tapo = _mapping(exporters.setdefault("tapo", {}), "exporter.exporters.tapo")
    tapo.setdefault("devices", [])
    if tapo.get("prometheus_options") is None:
        tapo["prometheus_options"] = {}
    options = _mapping(tapo["prometheus_options"], "exporter.exporters.tapo.prometheus_options")
    options.setdefault("scrape_timeout", 10.0)
    return exporter, tapo, options


def _apply_overrides(configuration: dict[str, object], overrides: dict[str, str]) -> None:
    """Apply supported nonempty environment overrides to their native settings.

    Raises
    ------
    ValueError
        If a port override cannot be converted to an integer.
    """
    for name, path in OVERRIDE_PATHS.items():
        value = overrides.get(name)
        if not value:
            continue
        converted: object = value
        if name in PORT_OVERRIDES:
            try:
                converted = int(value)
            except ValueError as error:
                message = f"{name} must be an integer port."
                raise ValueError(message) from error
        elif name == "TAPO_PLUG_DEVICES":
            converted = value.replace(",", " ").split()
            if not converted:
                message = "TAPO_PLUG_DEVICES must contain at least one host when nonempty."
                raise ValueError(message)
        parent = configuration
        for key in path[:-1]:
            parent = _mapping(parent.get(key), "Configuration section")
        parent[path[-1]] = converted


def _port(value: object, location: str) -> int:
    """Check a native integer TCP port.

    Returns
    -------
    int
        The validated port.

    Raises
    ------
    ValueError
        If the value is not an integer TCP port.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PORT:
        message = f"{location} must be an integer between 1 and {MAX_PORT}."
        raise ValueError(message)
    return value


def _duration_seconds(value: object, location: str) -> float:
    """Parse positive Prometheus durations in descending unit order.

    Returns
    -------
    float
        Duration in seconds, within Prometheus's signed nanosecond range.

    Raises
    ------
    ValueError
        If the duration is invalid, zero, or outside Prometheus's supported range.
    """
    match = DURATION_PATTERN.fullmatch(value) if isinstance(value, str) and len(value) <= MAX_DURATION_LENGTH else None
    if match is not None:
        milliseconds = sum(
            int(part or 0) * multiplier for part, multiplier in zip(match.groups(), DURATION_MULTIPLIERS, strict=True)
        )
        if 0 < milliseconds <= MAX_DURATION_MILLISECONDS:
            return milliseconds / 1000
    message = f"{location} must be a positive Prometheus duration such as 30s or 1h30m."
    raise ValueError(message)


def _retention_size(value: object) -> None:
    """Check the byte units accepted by Prometheus's retention size flag.

    Raises
    ------
    ValueError
        If the retention size is invalid or outside a signed 64-bit byte count.
    """
    match = SIZE_PATTERN.fullmatch(value) if isinstance(value, str) and len(value) <= MAX_SIZE_LENGTH else None
    if match is not None:
        size = float(match[1]) * 1024 ** SIZE_UNITS.index(match[2])
        if math.isfinite(size) and 0 <= size <= (1 << 63) - 1:
            return
    message = "prometheus.retention_size must use byte units such as 10GB (0B disables the size limit)."
    raise ValueError(message)


def _host(value: object) -> bool:
    """Recognize one literal IP address or DNS hostname without doing network I/O.

    Returns
    -------
    bool
        Whether the value is one nonempty host.
    """
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        labels = value.removesuffix(".").split(".")
        return len(value) <= MAX_HOST_LENGTH and all(
            len(label) <= MAX_LABEL_LENGTH and HOST_LABEL.fullmatch(label) is not None for label in labels
        )
    else:
        return True


def _validate_grafana(options: dict[str, object]) -> None:
    """Validate the UI address without resolving DNS or binding sockets.

    Raises
    ------
    ValueError
        If a host address or port is invalid.
    TypeError
        If the bind address is not text.
    """
    _require_keys(options, GRAFANA_KEYS, "grafana")
    _port(options["port"], "grafana.port")
    value = options["bind_address"]
    if not isinstance(value, str):
        message = "grafana.bind_address must be an IPv4 or IPv6 address."
        raise TypeError(message)
    try:
        options["bind_address"] = str(ipaddress.ip_address(value.removeprefix("[").removesuffix("]")))
    except ValueError as error:
        message = "grafana.bind_address must be an IPv4 or IPv6 address."
        raise ValueError(message) from error


def _positive_integer(value: object, location: str) -> None:
    """Check native options that require a positive integer.

    Raises
    ------
    ValueError
        If the setting is not a positive integer.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        message = f"{location} must be a positive integer."
        raise ValueError(message)


def _positive_seconds(value: object, location: str) -> float:
    """Check native timeouts without accepting boolean or nonfinite values.

    Returns
    -------
    float
        Positive timeout in seconds.

    Raises
    ------
    ValueError
        If the setting is not a finite positive number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        message = f"{location} must be a positive finite number of seconds."
        raise ValueError(message)
    return float(value)


def _validate_discovery(value: object) -> None:
    """Check advertised discovery settings while leaving native defaults implicit."""
    if value is None:
        return
    discovery = _mapping(value, "exporter.exporters.tapo.discovery_options")
    _positive_integer(discovery.get("discovery_packets", 3), "discovery_options.discovery_packets")
    _positive_seconds(discovery.get("discovery_timeout", 5), "discovery_options.discovery_timeout")
    if discovery.get("timeout") is not None:
        _positive_seconds(discovery["timeout"], "discovery_options.timeout")
    if discovery.get("port") is not None:
        _port(discovery["port"], "discovery_options.port")


def _validate_native(configuration: dict[str, object], *, require_devices: bool) -> float:
    """Validate shared native exporter settings while retaining other native options.

    Returns
    -------
    float
        Maximum exporter wait for a live refresh, in seconds.

    Raises
    ------
    ValueError
        If a shared exporter setting is invalid.
    """
    exporter, tapo, options = _native_sections(configuration)
    _port(exporter["prometheus_port"], "exporter.prometheus_port")
    level = exporter["log_level"]
    if not isinstance(level, str) or level.upper() not in LOG_LEVELS:
        message = "exporter.log_level must be a standard Python logging level."
        raise ValueError(message)
    exporter["log_level"] = level.upper()
    devices = tapo["devices"]
    if not isinstance(devices, list) or any(not _host(host) for host in devices) or (require_devices and not devices):
        message = "exporter.exporters.tapo.devices must list individual IPs or hostnames; at least one is required."
        raise ValueError(message)
    _positive_integer(tapo.get("max_concurrent_devices", 10), "exporter.exporters.tapo.max_concurrent_devices")
    if options.get("refresh_interval") is not None:
        _positive_integer(options["refresh_interval"], "prometheus_options.refresh_interval")
    _validate_discovery(tapo.get("discovery_options"))
    return _positive_seconds(options["scrape_timeout"], "prometheus_options.scrape_timeout")


def _validate(configuration: dict[str, object], *, require_devices: bool) -> None:
    """Check settings and timeout relationships before any runtime files exist.

    Raises
    ------
    ValueError
        If settings, durations, or the shared scrape budget are invalid.
    """
    _validate_tree(configuration)
    _require_keys(configuration, {"grafana", "prometheus", "exporter"}, "config/stack.yaml")
    _validate_grafana(_mapping(configuration["grafana"], "grafana"))
    prometheus = _mapping(configuration["prometheus"], "prometheus")
    _require_keys(prometheus, PROMETHEUS_KEYS, "prometheus")
    timings = {
        key: _duration_seconds(prometheus[key], f"prometheus.{key}") for key in PROMETHEUS_KEYS - {"retention_size"}
    }
    _retention_size(prometheus["retention_size"])
    exporter_wait = _validate_native(configuration, require_devices=require_devices)
    if not exporter_wait < timings["scrape_timeout"] <= timings["scrape_interval"]:
        message = "Require exporter scrape_timeout < Prometheus scrape_timeout <= scrape_interval."
        raise ValueError(message)


def load_configuration(
    directory: Path, overrides: dict[str, str], *, require_devices: bool = True
) -> dict[str, object]:
    """Load unified YAML and apply supported nonempty environment overrides.

    Parameters
    ----------
    directory : Path
        Repository checkout containing config/stack.yaml.
    overrides : dict[str, str]
        Effective environment values resolved by the caller; credentials are ignored.
    require_devices : bool
        Require at least one explicit host. Disable only for offline rendering/tests.

    Returns
    -------
    dict[str, object]
        Validated grafana, prometheus and native exporter settings.
    """
    configuration = _read_mapping(directory / "config" / "stack.yaml", "config/stack.yaml")
    _validate_tree(configuration)
    _native_sections(configuration)
    _apply_overrides(configuration, overrides)
    _validate(configuration, require_devices=require_devices)
    return configuration


def _prometheus_document(directory: Path, configuration: dict[str, object]) -> dict[str, object]:
    """Derive scrape configuration from the repository template.

    Returns
    -------
    dict[str, object]
        Prometheus document with synchronized timing and exporter target.

    Raises
    ------
    ValueError
        If the exporter job or target structure is missing or ambiguous.
    TypeError
        If the scrape job list is not a sequence.
    """
    document = _read_mapping(directory / "prometheus" / "prometheus.yml", "Prometheus template")
    options = _mapping(configuration["prometheus"], "prometheus")
    global_options = _mapping(document.get("global"), "Prometheus global settings")
    for name in ("scrape_interval", "scrape_timeout", "evaluation_interval"):
        global_options[name] = options[name]
    jobs = document.get("scrape_configs")
    if not isinstance(jobs, list):
        message = "The Prometheus template must contain scrape_configs."
        raise TypeError(message)
    exporter_jobs = [
        _mapping(job, "Prometheus job")
        for job in jobs
        if isinstance(job, dict) and job.get("job_name") == "pyprom-exporters"
    ]
    if len(exporter_jobs) != 1:
        message = "The Prometheus template must contain exactly one pyprom-exporters job."
        raise ValueError(message)
    exporter = _mapping(configuration["exporter"], "exporter")
    groups = exporter_jobs[0].get("static_configs")
    if not isinstance(groups, list) or len(groups) != 1:
        message = "The pyprom-exporters template job must contain exactly one static target group."
        raise ValueError(message)
    group = _mapping(groups[0], "Exporter static target group")
    group["targets"] = [f"exporter:{exporter['prometheus_port']}"]
    for job in jobs:
        if isinstance(job, dict) and job.get("job_name") == "prometheus":
            job.pop("scrape_timeout", None)
    _validate_tree(document)
    return document


def _datasource_document(directory: Path, configuration: dict[str, object]) -> dict[str, object]:
    """Derive Grafana's Prometheus interval from unified scrape settings.

    Returns
    -------
    dict[str, object]
        Datasource provisioning document with a synchronized interval.

    Raises
    ------
    ValueError
        If the template does not define exactly one provisioned Prometheus source.
    TypeError
        If the datasource list is not a sequence.
    """
    path = directory / "grafana" / "provisioning" / "datasources" / "prometheus.yaml"
    document = _read_mapping(path, "Grafana datasource template")
    sources = document.get("datasources")
    if not isinstance(sources, list):
        message = "The datasource template must define the prometheus datasource."
        raise TypeError(message)
    matching = [
        _mapping(source, "Datasource")
        for source in sources
        if isinstance(source, dict) and source.get("uid") == "prometheus"
    ]
    if len(matching) != 1:
        message = "The datasource template must define exactly one prometheus datasource."
        raise ValueError(message)
    data = _mapping(matching[0].setdefault("jsonData", {}), "Datasource jsonData")
    data["timeInterval"] = _mapping(configuration["prometheus"], "prometheus")["scrape_interval"]
    _validate_tree(document)
    return document


def _verify_existing(destination: Path, files: dict[str, bytes]) -> None:
    """Refuse to change an existing rendered configuration generation.

    Raises
    ------
    ValueError
        If existing output was replaced or modified.
    """
    if destination.is_symlink() or not destination.is_dir():
        message = "The runtime configuration destination is not a regular directory."
        raise ValueError(message)
    for name, content in files.items():
        path = destination / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            message = "An existing runtime configuration generation has been modified; it will not be overwritten."
            raise ValueError(message)


def _publish_files(directory: Path, files: dict[str, bytes]) -> Path:
    """Publish all files together so readers cannot observe a partial generation.

    Returns
    -------
    Path
        Content-addressed runtime directory, reusable across equivalent renders.

    Raises
    ------
    ValueError
        If the runtime root is a symlink or an existing generation was changed.
    OSError
        If a complete generation cannot be written or published.
    """
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8") + b"\0" + content + b"\0")
    runtime = directory / ".runtime"
    if runtime.is_symlink():
        message = "The .runtime directory must not be a symlink."
        raise ValueError(message)
    runtime.mkdir(mode=0o755, parents=True, exist_ok=True)
    runtime.chmod(0o755)
    destination = runtime / digest.hexdigest()
    if destination.exists() or destination.is_symlink():
        _verify_existing(destination, files)
        return destination
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=runtime) as staging_name:
        staging = Path(staging_name)
        staging.chmod(0o755)
        for name, content in files.items():
            with (staging / name).open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            (staging / name).chmod(0o644)
        try:
            staging.rename(destination)
        except OSError:
            # Another process may have published this identical generation first.
            if not destination.exists():
                raise
            _verify_existing(destination, files)
    return destination


def render_configuration(directory: Path, configuration: dict[str, object]) -> Path:
    """Publish native exporter, Prometheus and datasource files without credentials.

    Parameters
    ----------
    directory : Path
        Checkout containing the source templates and ignored .runtime directory.
    configuration : dict[str, object]
        Unified validated settings; input mappings and source files remain unchanged.

    Returns
    -------
    Path
        Immutable content-addressed directory containing all three rendered YAML files.
    """
    effective = copy.deepcopy(configuration)
    _validate(effective, require_devices=False)
    documents = {
        "exporter.yaml": _mapping(effective["exporter"], "exporter"),
        "prometheus.yml": _prometheus_document(directory, effective),
        "datasource.yaml": _datasource_document(directory, effective),
    }
    files = {name: yaml.safe_dump(document, sort_keys=True).encode("utf-8") for name, document in documents.items()}
    return _publish_files(directory, files)


def compose_environment(configuration: dict[str, object], runtime_dir: Path) -> dict[str, str]:
    """Return only resolved noncredential values required by the Compose template.

    Parameters
    ----------
    configuration : dict[str, object]
        Effective configuration returned by load_configuration.
    runtime_dir : Path
        Immutable directory returned by render_configuration.

    Returns
    -------
    dict[str, str]
        Environment overrides synchronizing Compose with the rendered configuration.
    """
    effective = copy.deepcopy(configuration)
    _validate(effective, require_devices=False)
    exporter, tapo, _ = _native_sections(effective)
    grafana = _mapping(effective["grafana"], "grafana")
    prometheus = _mapping(effective["prometheus"], "prometheus")
    address = str(grafana["bind_address"])
    return {
        "GRAFANA_BIND_ADDRESS": f"[{address}]" if ":" in address else address,
        "GRAFANA_PORT": str(grafana["port"]),
        "PROMETHEUS_RETENTION_TIME": str(prometheus["retention_time"]),
        "PROMETHEUS_RETENTION_SIZE": str(prometheus["retention_size"]),
        "PROMETHEUS_PORT": str(exporter["prometheus_port"]),
        "PYPROM_EXPORTERS_LOG_LEVEL": str(exporter["log_level"]),
        "TAPO_PLUG_DEVICES": " ".join(cast("list[str]", tapo["devices"])),
        "PYPROM_RUNTIME_DIR": str(runtime_dir.resolve()),
    }
