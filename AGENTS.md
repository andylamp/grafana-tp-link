# AGENTS

## Project and ownership

This repository runs `pyprom-exporters`, Prometheus and Grafana as a provisioned Compose stack.
The Python package supplies the `power-monitor` checkout utility; it does not implement device protocols.
Device collection belongs to the separate `pyprom-exporters` project.

- `compose.yaml`: pinned images, service configuration, health checks and persistent storage.
- `config/stack.yaml`: all noncredential user settings, with native exporter options nested under `exporter`.
- `prometheus/`: native configuration templates, alert rules and fixtures.
- `grafana/provisioning/`: native datasource and dashboard provisioning templates.
- `.runtime/`: ignored generated native configurations addressed by their content digest.
- `dash.json`: portable standalone dashboard with built-in Grafana panels.
- `src/grafana_tp_link/`: CLI initialization and Compose lifecycle commands.
- `tests/`: unit/contract tests and an opt-in isolated Docker integration suite.
- `README.md` and `docs/migration.md`: operation and preservation of existing installations.

Work on descriptive `feat/`, `bugfix/` or `chore/` branches, preserving the default branch.
Do not deploy changes, probe real plugs or change existing data while validating code.
Use fake devices and isolated integration resources. Keep audit notes in ignored `scratch/`
and generated reports in ignored `report/`.

## Configuration and data

`power-monitor init` creates a private `.env` with a generated password and preserves existing settings.
Do not print or commit credentials. Explicit device hosts avoid dependence on broadcast discovery through Docker.
Keep `.env.example` free of usable credentials.

The single user-facing configuration file is `config/stack.yaml`. Device inventory lives at
`exporter.exporters.tapo.devices`, with one IP address or hostname per entry and optional inline comments.
Grafana service settings and Prometheus retention/scrape settings live in the `grafana` and `prometheus` sections.
Global `--config PATH` selects one complete stack YAML, resolved relative to the checkout or as an absolute path.
Never merge a selected file with the default or a leftover native `config/exporter.yaml`; missing selections must fail.
Native exporter-only files need an explicit migration into the stack YAML's `exporter` section.
`grafana.data_directory` and `prometheus.data_directory` optionally select host storage; null or omission retains the
existing named volumes. Resolve relative data paths from the checkout, even for a configuration outside it.
Never create, chown, copy or delete user storage directories. Generated bind mounts must set `create_host_path: false`.
Support corresponding nonempty `GRAFANA_DATA_DIRECTORY` and `PROMETHEUS_DATA_DIRECTORY` environment overrides.
Normalize the literal `localhost` bind address to loopback without changing the source YAML.
Keep only credentials active in `.env.example`; optional noncredential overrides must be commented out.
Compose resolves shell variables ahead of `.env`; nonempty resolved overrides replace YAML, while empty values use YAML.
A device override replaces the complete list; whitespace/comma-only overrides are invalid.

`check` and `up` validate the effective configuration and require at least one explicit host. Render credential-free native
service files into ignored `.runtime/<digest>/`, keeping exporter targets and datasource intervals synchronized.
Never rewrite source YAML: preserve user comments and formatting. Mount generated configuration read-only and retain
`--no-write-config`. A configuration digest change must produce changed mount paths, so `up` applies YAML changes by
recreating affected containers while preserving persistent data; do not require manual restarts.

The utility always loads base `compose.yaml` first; repeatable global `--compose-file` options add files in the supplied
order, resolving relative paths against the checkout. For `check`/`up`, insert the generated `compose.storage.yaml`
after the base and before explicit override files. Honor explicit overrides for every lifecycle command, including reset.
Recovery commands (`down`, `reset`, `status`, `logs`, `pull`) must work without `.env` or valid user YAML and must retain
project identity where available through `.env` or the shell's `COMPOSE_PROJECT_NAME`.

`down` and the wrapper's `-r` must preserve persistent volumes. The explicit `reset --yes` command is the exception:
it removes the selected Compose project's containers, non-external networks and managed data volumes.
Reset must require `--yes`, preserve `.env`, source files, images, external volumes and host bind directories,
and must not restart services automatically. `make reset` intentionally supplies `--yes`.
Do not execute reset against an existing deployment as part of validation; use isolated integration resources.
Do not reintroduce blanket prune commands or deletion of host data directories.
Database migration is a separately documented operation on backed-up copies; do not perform it during development.
Keep the exporter wait below the Prometheus timeout, and that timeout at or below the scrape interval.

Dashboard metrics use `host` and `alias`, with Prometheus adding `job` and `instance`.
Energy is in Wh; daily/monthly gauges reset and must not be treated as counters.
Scrape health and scrape age do not establish device-reading freshness. Preserve missing data instead of replacing it
with zero, and retain datasource portability and regex-safe multiple-device filters.

## Development checks

Use Python 3.11+, uv and the checked-in lockfile. Ruff enables all stable and preview rules; ty checks types.
Markdown follows `.markdownlint-cli2.jsonc`, shared with `pyprom-exporters`.

After changes, run:

```sh
uv run --locked pytest
uv run --locked prek run --all-files
```

Tests run with up to four workers by default. Use `-n 0` for serial debugging.
For stack, provisioning, image or dashboard-query changes, also run the isolated integration suite when Docker is available:

```sh
make integration
```

This enables `RUN_STACK_INTEGRATION=1` and runs integration tests serially against fake metrics/devices.
If unavailable, report that limitation explicitly. Validate image version and digest updates together, and keep
configuration, dashboard contracts and documented defaults consistent.
