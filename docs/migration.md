# Migrating an existing installation

This migration replaces `fffonion/tplink-plug-exporter` with `pyprom-exporters` and provisions a new dashboard for
Grafana 13.2.2 and Prometheus 3.15.0. The default Compose stack uses new named volumes. It does not inspect, upgrade,
copy or remove data from an older installation.

For an existing deployment, preserve the original data and rehearse any database upgrade on a separate copy.
The simplest migration is fresh Grafana/Prometheus storage, retaining the old deployment's backup for historical access.
Preserving Prometheus history and preserving Grafana accounts/dashboards are independent choices.

## Inventory and preserve the old deployment

The old repository's `-r` path deleted bind-mounted data directories and pruned Docker resources.
**Do not run an old copy of that removal script.** The replacement wrapper's `-r` maps to a data-preserving Compose
`down`, but it cannot restore anything previously deleted.

Before changing running services:

1. Record the exact image versions/digests and container names. The old `latest` tag does not identify the installed version.
1. Record each container's data mounts and save the old Compose file, Prometheus configuration, Grafana configuration,
   custom dashboards and plugins. Preserve any Grafana encryption settings needed to read saved datasource credentials.
1. Stop the old Grafana and Prometheus processes before copying SQLite and TSDB files.
1. Copy their complete data directories to a separate backup location, retaining ownership and permissions.
   Keep an untouched backup and use another copy for upgrade rehearsals.

For example, inspect metadata without dumping environment variables containing credentials:

```sh
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker inspect --format '{{.Image}}' grafana-local
docker inspect --format '{{json .Mounts}}' grafana-local
docker inspect --format '{{.Image}}' prometheus-local
docker inspect --format '{{json .Mounts}}' prometheus-local
```

`grafana-local` and `prometheus-local` are names used by the old project; substitute the actual names from your inventory.
The old README used `/usr/grafana-container-data` and `/usr/prometheus-container-data` as example bind locations.
Verify your mounts instead of assuming those paths apply.

After stopping the corresponding containers, copy the actual directories to your backup destination using a tool that
preserves ownership and permissions, such as `cp -a` or an appropriate filesystem snapshot. Keep the backup private:
Grafana configuration and databases can contain credentials. For externally hosted Grafana databases, use the database's
backup procedure instead of copying SQLite files. See [Grafana's backup guide][grafana-backup].

## Start with fresh storage

After preserving the old installation, follow the [quick start](../README.md#quick-start).
Stop old services first if they already occupy the desired Grafana host port. The new stack does not automatically stop
old containers or remove their data.

The new named volumes retain data across `power-monitor down` and `power-monitor up`.
Their names are scoped to the new Compose project, `tp-link-monitoring`, which differs from the old launcher's
`grafana-tp-link`. Do not override `COMPOSE_PROJECT_NAME` with an existing deployment's name during rehearsal;
Compose uses that identity to select containers for update and removal.
A fresh Grafana database uses the username and generated password in `.env`.

This route creates new time series and a new Grafana database. It leaves your old history, accounts and custom dashboards
in their preserved storage. Export individual old dashboards for reference if needed; legacy panel types and old metric
queries still need migration before they work with the new services.

## Preserve Prometheus history on a copy

For an old Prometheus 2.x installation, first rehearse upgrading the copied data with Prometheus 2.55 before opening it
with Prometheus 3.x. Check readiness, historical queries and logs at that step. The Prometheus project recommends this
bridge; Prometheus 3 data cannot be read by versions older than 2.55. Read the
[Prometheus 3 migration guide][prometheus-migration] and [3.0 release guidance][prometheus-release].

Review retention before opening the copied store. This stack defaults to three years or 10 GB of stored blocks,
whichever limit is reached first; shorter limits can remove historical data from the working copy.
Keep the original backup outside the active data directory.

Create a separate working directory such as `data/prometheus`, then copy the stopped backup's complete TSDB data into it.
For a verified backup stored at `/path/to/prometheus-backup`, for example:

```sh
mkdir -p data
mkdir data/prometheus && sudo cp -a /path/to/prometheus-backup/. data/prometheus/
```

Replace the example backup path with your actual stopped TSDB backup. Stop if `data/prometheus` already exists;
choose a new working directory instead of merging an old rehearsal with the backup.
Do not bind-mount the only backup or the old installation's live directory. Ensure the directory is writable by the
Prometheus image's configured user. Inspect the pinned image instead of reusing the old project's fixed user ID:

```sh
docker image inspect --format '{{.Config.User}}' \
  quay.io/prometheus/prometheus:v3.15.0@sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e
```

Adjust ownership of the working copy for your Docker environment; rootless Docker and user namespaces can change how
container IDs map onto the host. Do not recursively change ownership of the original backup.

After the copied store has passed the staged upgrade, create an ignored `compose.override.yaml`:

```yaml
services:
  prometheus:
    volumes:
      - type: bind
        source: ./data/prometheus
        target: /prometheus
        bind:
          create_host_path: false
```

The override replaces the storage mount with the matching container target while retaining the read-only configuration
mounts. `create_host_path: false` makes a missing working directory fail rather than silently create empty storage.
Compose's [merge rules][compose-merge] define this behavior.

Pass both files explicitly:

```sh
docker compose --env-file .env -f compose.yaml -f compose.override.yaml config --quiet
docker compose --env-file .env -f compose.yaml -f compose.override.yaml up -d --wait
```

The `power-monitor` utility selects `compose.yaml` explicitly and does not load this override.
For a deployment using it, continue passing both files to lifecycle commands:

```sh
docker compose --env-file .env -f compose.yaml -f compose.override.yaml ps
docker compose --env-file .env -f compose.yaml -f compose.override.yaml logs --tail 100 prometheus
docker compose --env-file .env -f compose.yaml -f compose.override.yaml down
```

Confirm that historical queries still work and that new `current_consumption` samples arrive before retiring the old
installation. Keep the original backup for rollback; opening an upgraded store with an older binary is not a substitute
for restoring a compatible backup.

## Preserve Grafana accounts and custom dashboards on a copy

A new provisioned dashboard does not require migrating the old Grafana database.
Use fresh Grafana storage unless you also need its existing users, organizations, dashboards or settings.

For an old Grafana 6/7-era database, rehearse upgrades on a copy, working through the major-version upgrade guides and
checking each stage before continuing. Record the exact image used at every step, retain a backup before the next stage,
and review plugin compatibility and removed features. Grafana's current support for upgrades between supported releases
does not establish that an arbitrary legacy database and plugin set can jump directly to this version.
Consult the [official upgrade guides][grafana-upgrade].

Use an isolated rehearsal deployment for these intermediate versions. Do not start the new stack's Grafana 13 image
against an untested old database. Retain the relevant Grafana configuration and encryption key when migrating stored
credentials, and follow the backup guide for MySQL/PostgreSQL deployments. Allow disk space for database migrations.
Explicitly carry any required authentication, external database and encryption settings into the local Compose override
using Grafana environment settings or a read-only configuration mount. For example, preserve a custom
`GF_SECURITY_SECRET_KEY` through private local configuration. The base stack does not load the old `grafana.ini`;
copying its file or the database alone does not apply those settings.

Once a copied SQLite data directory is verified with the target version, put it at an ignored path such as `data/grafana`.
Add this service entry alongside any Prometheus override:

```yaml
services:
  grafana:
    volumes:
      - type: bind
        source: ./data/grafana
        target: /var/lib/grafana
        bind:
          create_host_path: false
```

Merge both service entries into one `services` mapping if preserving both databases.
Confirm that the copied data is writable by the pinned Grafana image's configured user and that the resolved mounts still
include the repository's datasource provisioning, dashboard provider and `dash.json`.
Use the same explicit two-file Compose commands above for this deployment.

Changing `GRAFANA_ADMIN_PASSWORD` in `.env` does not reset an existing database's administrator password.
Use the existing account or Grafana's documented reset procedure. Review datasource UID `prometheus`, folder UID
`power-monitoring` and dashboard UID `tp-link-power` for collisions before allowing provisioning to update those resources.
Keep the old dashboard under a separate UID if you want to retain it for historical queries.

## Metric and dashboard changes

Prometheus now scrapes one aggregate endpoint at `exporter:8090/metrics` with job `pyprom-exporters`.
Explicit device hosts belong in `TAPO_PLUG_DEVICES`;
the old per-device scrape target/relabel configuration is no longer used.
The endpoint probes devices concurrently. Its scrape timeout is 25 seconds, its interval is 30 seconds, and the exporter's
live-refresh wait is 20 seconds by default.

| Previous dashboard concept | New metric or behavior |
| --- | --- |
| `kasa_power_load` | `current_consumption`, in W |
| `kasa_current` | `current_current`, in A, where supported |
| `kasa_voltage` | `current_voltage`, in V, where supported |
| `kasa_online` | No direct replacement; exporter scrape health and reporting-series count are separate concepts |
| `kasa_relay_state` | Not exposed by this exporter |
| Sample-summed hourly/yearly energy | Device-reported `current_consumption_today` and `current_month_consumption`, in Wh |
| Alias-only series labels | `host` and `alias`, plus Prometheus `job` and `instance` |

Historical `kasa_*` series are not renamed, rewritten or combined with the new series.
They remain queryable for as long as the preserved Prometheus storage and its retention allow.
The new dashboard displays new exporter metrics only. Daily/monthly energy gauges reset on the plug's calendar and cannot
be treated as monotonic counters or a reliable reconstruction of old hourly/yearly estimates.

To reuse only the dashboard elsewhere, import [dash.json](../dash.json) as a standalone dashboard.
Select its Prometheus source using **Data source**. It has no API request wrapper or external panel plugins.
Select the appropriate job, exporter, host and device filters for your installation.

## Validate before retiring the old deployment

- Verify service health and review exporter, Prometheus and Grafana logs.
- Confirm that configured plugs produce the expected native units and that repeated aliases remain distinguishable.
- Check a missing or unsupported reading is shown as missing rather than zero.
- Confirm new and retained historical samples separately when preserving Prometheus data.
- Verify Grafana login, datasource provisioning and the new dashboard.
- Keep backups and the recorded old image digests until the migration is accepted.

The included alerts have no configured notification delivery. They can identify failed exporter scrapes and missing
reported power series, but cannot prove that a successfully returned snapshot is fresh. See the
[dashboard and alert limitations](../README.md#reading-and-reusing-the-dashboard).

[grafana-backup]: https://grafana.com/docs/grafana/latest/administration/back-up-grafana/
[grafana-upgrade]: https://grafana.com/docs/grafana/latest/upgrade-guide/
[prometheus-migration]: https://prometheus.io/docs/prometheus/latest/migration/
[prometheus-release]: https://prometheus.io/blog/2024/11/14/prometheus-3-0/
[compose-merge]: https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/
