# AGENTS

## Project and ownership

This repository runs `pyprom-exporters`, Prometheus and Grafana as a provisioned Compose stack.
The Python package supplies the `power-monitor` checkout utility; it does not implement device protocols.
Device collection belongs to the separate `pyprom-exporters` project.

- `compose.yaml`: pinned images, service configuration, health checks and persistent storage.
- `config/exporter.yaml`: live probing, timeouts and concurrency; hosts and credentials come from `.env`.
- `prometheus/`: scrape configuration and alert rules.
- `grafana/provisioning/`: datasource and dashboard providers.
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

`down` and the wrapper's `-r` must preserve persistent volumes. The explicit `reset --yes` command is the exception:
it removes the selected Compose project's containers, non-external networks and managed data volumes.
Reset must require `--yes`, preserve `.env`, source files, images, external volumes and host bind directories,
and must not restart services automatically. `make reset` intentionally supplies `--yes`.
Do not execute reset against an existing deployment as part of validation; use isolated integration resources.
Do not reintroduce blanket prune commands or deletion of host data directories.
Database migration is a separately documented operation on backed-up copies; do not perform it during development.
Keep the exporter wait below the Prometheus timeout, and that timeout below the scrape interval.

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
