# TP-Link power monitoring

Monitor TP-Link Tapo and Kasa energy-monitoring plugs with
[pyprom-exporters](https://github.com/andylamp/pyprom-exporters), Prometheus and Grafana.
The stack provisions its datasource, dashboard and alert rules from this repository.
It probes devices on scrape by default and keeps monitoring data in persistent Docker volumes.

**Already running the older stack?** Read [Migrating an existing installation](docs/migration.md)
before starting these containers. The new stack uses fresh volumes by default and does not migrate old databases.

## What is included

| Component | Pinned version | Purpose |
| --- | --- | --- |
| pyprom-exporters | 0.2.0 | Concurrent Tapo/Kasa discovery and live measurements |
| Prometheus | 3.15.0 | Time-series storage, scraping and alert evaluation |
| Grafana | 13.2.2 | Provisioned power, energy and health dashboard |

Images are pinned by version and digest in [compose.yaml](compose.yaml). The exporter replaces
`fffonion/tplink-plug-exporter`; its metrics and device configuration are different.

The dashboard includes current power, daily/monthly energy, voltage, current, Wi-Fi signal and exporter diagnostics.
Only Grafana's built-in panels are used. Datasource, job, exporter, host and device filters make it portable across
installations, including devices with identical aliases.

![Provisioned dashboard with simulated device readings](assets/power-dashboard.png)

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
[Docker with the Compose plugin](https://docs.docker.com/compose/install/).
The Python utility supports Python 3.11 and newer; uv can provision Python when needed.
The Docker host must be able to reach your plugs on the LAN.

```sh
git clone https://github.com/andylamp/grafana-tp-link.git
cd grafana-tp-link
uv sync --locked
uv run --locked power-monitor init
```

`init` creates a Git-ignored `.env` with private permissions and a randomly generated Grafana admin password.
It prints no password and preserves an existing `.env`.

Edit `.env` before continuing:

- Set `TAPO_PLUG_DEVICES` to explicit plug IP addresses or resolvable hostnames, separated by spaces or commas.
  Reserve their DHCP addresses where possible. Docker bridge networking does not provide reliable LAN broadcast discovery.
- Set `TP_LINK_USERNAME` and `TP_LINK_PASSWORD` when your devices require Tapo account authentication.
  Older Kasa/HS110 devices may work with both empty.
- Keep or change the generated `GRAFANA_ADMIN_PASSWORD`. Read it locally from `.env` when signing in.
  Single-quote values containing `$` or `#` so Compose treats those characters literally.

Then validate and start the stack:

```sh
uv run --locked power-monitor check
uv run --locked power-monitor up
```

Open [Grafana](http://127.0.0.1:3000/) and sign in as `admin`, or your configured `GRAFANA_ADMIN_USER`.
The **TP-Link · Power & Energy** dashboard is provisioned automatically and selected as the home dashboard.
No API registration script, manual datasource creation or dashboard upload is required.

`check` validates Compose and runs the pinned Prometheus image's `promtool` against the configuration and alert rules.
It also runs the alert-rule fixtures. It does not start the exporter or probe plugs.
`up` waits for container health checks; those checks establish service readiness,
not that every configured plug has produced a reading.

## Operating the stack

Run commands from this checkout. From another directory, specify both the uv project and stack location:

```sh
uv run --locked --project /path/to/grafana-tp-link \
  power-monitor --directory /path/to/grafana-tp-link status
```

| Command | Behavior |
| --- | --- |
| `uv run --locked power-monitor init` | Create `.env` once without replacing local settings |
| `uv run --locked power-monitor check` | Validate Compose, Prometheus configuration and rules |
| `uv run --locked power-monitor up` | Create/update services and wait for health checks |
| `uv run --locked power-monitor status` | Show service status |
| `uv run --locked power-monitor logs --follow exporter` | Follow exporter logs |
| `uv run --locked power-monitor down` | Stop this stack while retaining persistent volumes |
| `uv run --locked power-monitor reset --yes` | Delete this project's containers and data volumes |
| `uv run --locked power-monitor pull` | Download the pinned images |

`make init`, `make up`, `make down`, `make status`, `make logs` and `make pull` provide equivalent shortcuts.
The compatibility wrapper `./grafana-tp-link-docker -i` starts services and `-r` stops them while preserving data.
Use `power-monitor init` first. The wrapper's old destructive removal behavior has been removed;
deleting monitoring data now requires the explicit reset command below.

The Compose project is `tp-link-monitoring`, distinct from the legacy `grafana-tp-link` project.
`status`, `logs`, `down`, `reset --yes` and `pull` also work without `.env`. If you originally set a custom
`COMPOSE_PROJECT_NAME`, retain that name in `.env` or export it in your shell to address the same stack.
`check` and `up` require configured device hosts and validate the resolved settings before creating containers.

Pulling downloads the versions recorded in `compose.yaml`; it does not select newer releases automatically.
Review version and digest changes together, consult upstream upgrade notes, back up data, then run `pull` and `up`.

### Reset for a fresh run

**Reset deletes Prometheus history and Grafana's stored accounts, settings and dashboard copies.**
Use it when you intend to discard this stack's persistent data, such as during local iteration:

```sh
uv run --locked power-monitor reset --yes
uv run --locked power-monitor up
```

The equivalent Makefile shortcut supplies the required confirmation flag for you:

```sh
make reset && make up
```

`reset` requires `--yes` and has no interactive prompt; without that flag it refuses to proceed.
It runs `docker compose down --volumes --remove-orphans` for the selected Compose project, removing its containers
(including orphaned services), non-external networks, declared named volumes and attached anonymous volumes.
It does not run a global Docker prune or restart services automatically.

Your `.env`, tracked configuration, source files and downloaded images remain in place.
External volumes and host bind-mounted directories are retained. With the standard named-volume configuration,
the next `up` creates empty storage and provisions the checked-in dashboard and datasource again, using the admin
credentials retained in `.env`. A deployment using bind-mounted data keeps that data; reset does not erase its host files.

Reset also works if `.env` is missing. Preserve or export a custom `COMPOSE_PROJECT_NAME` to select the intended project;
without that setting, the default project is selected. Recreate/configure `.env` before starting services again if needed.
For an ordinary stop that retains monitoring data, use `down` or the wrapper's `-r` instead.

## Configuration

[.env.example](.env.example) lists the local environment settings. Credentials are not written into the tracked
exporter configuration. Docker administrators can still inspect container environment variables; keep `.env` private.

| Setting | Default | Meaning |
| --- | --- | --- |
| `TAPO_PLUG_DEVICES` | Required | Explicit plug hosts, separated by spaces or commas |
| `TP_LINK_USERNAME`, `TP_LINK_PASSWORD` | Empty | Device account credentials when required |
| `PYPROM_EXPORTERS_LOG_LEVEL` | `INFO` | Exporter logging level |
| `GRAFANA_ADMIN_USER` | `admin` | Initial Grafana administrator name |
| `GRAFANA_ADMIN_PASSWORD` | Generated by `init` | Initial password for a new Grafana database |
| `GRAFANA_BIND_ADDRESS` | `127.0.0.1` | Host address serving Grafana |
| `GRAFANA_PORT` | `3000` | Host port serving Grafana |
| `PROMETHEUS_RETENTION_TIME` | `3y` | Maximum stored history by age |
| `PROMETHEUS_RETENTION_SIZE` | `10GB` | Maximum stored blocks by size |

Changing Grafana's initial admin settings does not reset accounts in an existing database.
Use Grafana's account-management or password-reset facilities for an existing installation.

Grafana binds to the local host by default. For remote access, configure a suitable bind address and your network's
access controls, or place it behind an authenticated TLS reverse proxy. Prometheus and the exporter are available only
on the Compose network; their ports are not published to the host.

Prometheus removes older blocks when either retention limit is reached. The size setting does not cap all disk use:
allow additional space for the write-ahead log, active samples and compaction. Volumes persist across `down` and `up`.
Do not use `docker compose down --volumes` when you want to retain the data.

### Scraping and device concurrency

The checked-in defaults are:

- Prometheus scrapes `exporter:8090/metrics` every 30 seconds, with a 25-second timeout.
- The exporter probes on scrape (`refresh_interval: null`) and waits up to 20 seconds for the refresh.
- Up to 10 device operations run concurrently. Individual device requests time out after 5 seconds.
- Overlapping exporter scrapes share an in-flight refresh. If its wait expires, the exporter can return an older snapshot.

Edit [config/exporter.yaml](config/exporter.yaml) for device concurrency and exporter timeouts.
Edit [prometheus/prometheus.yml](prometheus/prometheus.yml) for scraping and evaluation intervals.
Keep the exporter wait below Prometheus's scrape timeout and the scrape timeout below the scrape interval.
Keep Grafana's datasource interval in
[grafana/provisioning/datasources](grafana/provisioning/datasources) aligned with the scrape interval.

A positive `refresh_interval` enables background polling instead of live probing. Increase concurrency or polling
intervals based on observed device latency and LAN capacity; larger fleets and unreachable plugs can lengthen refreshes.
Run `power-monitor check` after changes and restart affected services to load updated configuration.
Compose `up` does not detect changed contents of bind-mounted configuration files. For example:

```sh
uv run --locked power-monitor check
docker compose --env-file .env -f compose.yaml restart exporter prometheus grafana
```

## Reading and reusing the dashboard

[dash.json](dash.json) is a standalone dashboard that can also be imported into another Grafana installation.
Choose **Dashboards → New → Import**, upload the file, and select a Prometheus source using the dashboard's
**Data source** variable. The provisioned source has UID `prometheus`; the dashboard UID is `tp-link-power`.
Choose the job and exporter filters if your scrape labels differ.

The provisioned dashboard is managed by its JSON file. Save customizations back to `dash.json`, or make a separate
Grafana copy with a different UID. Provisioning updates the managed dashboard from disk; UI changes are disabled for it.

| Metric | Native unit | Dashboard meaning |
| --- | --- | --- |
| `current_consumption` | W | Instantaneous device power |
| `current_voltage` | V | Device voltage, where supported |
| `current_current` | A | Device current, where supported |
| `current_rssi` | dBm | Received wireless signal strength, where supported |
| `current_consumption_today` | Wh | Energy accumulated on the plug's calendar day |
| `current_month_consumption` | Wh | Energy accumulated in the plug's calendar month |
| `tapo_discovered_devices` | Devices | Retained discovery inventory, not an online count |

Energy is already in Wh; Grafana scales its display to kWh when appropriate. Daily and monthly values are gauges that
reset on the plug's calendar. They are neither lifetime counters nor energy consumed during the dashboard's selected
range. The dashboard does not apply `rate`/`increase` to them or estimate energy from a fixed sampling cadence.

A blank reading means unavailable data, not zero. Totals include only selected devices that report the relevant metric.
The same physical plug monitored by multiple exporters can be counted more than once. Model and firmware capabilities
vary, so a working power reading does not imply that voltage, current, energy or Wi-Fi metrics are available.

**Exporter scrape status and scrape age describe Prometheus requests, not device freshness.** The exporter does not expose
per-device update timestamps, online status or relay state. A successful scrape can contain an older snapshot; inspect
exporter logs when values stop changing. Graphs preserve gaps, and queries hide device values when the exporter scrape
has failed.

### Alerts and troubleshooting

[prometheus/alerts.yml](prometheus/alerts.yml) evaluates three rules:

| Alert | Condition |
| --- | --- |
| `TapoExporterDown` | An exporter target fails scrapes for two minutes |
| `TapoNoPowerReadings` | A reachable exporter returns no power series for five minutes |
| `TapoPlugReadingsMissing` | A host seen within 24 hours has no current power series for five minutes |

These rules are evaluated in Prometheus. No Alertmanager or notification delivery is configured.
The missing-host rule cannot detect a plug that has never reported, or one absent for longer than its 24-hour history
window. Intentionally removing a previously observed host can trigger it. None of these rules can detect an old snapshot
that is still being returned successfully.

For missing readings, check `power-monitor status`, then `power-monitor logs exporter`.
Confirm explicit host addresses, reachability from Docker, credentials and the device's energy-monitoring support.
If Grafana cannot query Prometheus, inspect `power-monitor logs prometheus grafana` and datasource provisioning.

## Development

```sh
uv sync --locked
uv run --locked prek install
uv run --locked pytest
uv run --locked prek run --all-files
```

Pytest runs with up to four workers by default; use `-n 0` for serial debugging.
Docker integration tests are skipped unless explicitly enabled. To run them against the pinned images:

```sh
make integration
# Equivalent:
RUN_STACK_INTEGRATION=1 uv run --locked pytest -n 0 -m integration
```

The integration suite uses an isolated Compose project and fake devices/metrics. It does not require physical plugs
or real account credentials. It validates configuration, provisioning, dashboard queries and monitoring failure cases.
`make test` runs the regular tests; `make check` runs all prek hooks.

Ruff enables all stable and preview rules, ty checks Python, and Markdown uses the same
[markdownlint configuration](.markdownlint-cli2.jsonc) as `pyprom-exporters`.
Python dependencies and tools are locked in `uv.lock`; update them with `uv lock --upgrade`, then rerun checks.
Keep audit notes in ignored `scratch/` and generated reports in ignored `report/`.

## Repository layout

- `compose.yaml`: pinned Compose services, volumes and health checks.
- `config/exporter.yaml`: exporter behavior; credentials and hosts come from `.env`.
- `prometheus/`: scrape configuration and alert rules.
- `grafana/provisioning/`: datasource and dashboard provisioning.
- `dash.json`: portable Grafana dashboard.
- `src/grafana_tp_link/`: the `power-monitor` utility.
- `tests/`: configuration, dashboard, utility and isolated integration tests.
- `docs/migration.md`: preservation and upgrade guidance for an existing stack.
