# UPS2MQTT

`ups2mqtt` is your fast path to UPS observability and automation.

It delivers live UPS telemetry into MQTT and Home Assistant with a deployment model built for operators: simple Docker Compose runtime, practical web management UI, and optional HTTPS + Basic Auth via Caddy.

What makes it compelling:
1. Specialized high-fidelity drivers for APC and CyberPower.
2. Broad compatibility for additional UPS brands/models that implement RFC1628.
3. Clean MQTT publishing with Home Assistant discovery support.
4. Profile-based polling and device management designed for real production environments.
5. Standalone, low-friction architecture that avoids heavyweight platform lock-in.

If your goal is reliable, vendor-flexible UPS monitoring without custom integration work, this is built for that.

## Why Teams Choose UPS2MQTT

- Deploy fast: get from zero to live UPS telemetry in minutes with Docker Compose.
- Scale confidently: tunable polling, concurrent worker slots, and profile-based control for mixed device fleets.
- Work across vendors: specialized APC and CyberPower drivers, plus support for UPS models that follow RFC1628.
- Keep integrations clean: MQTT publishing with Home Assistant discovery and lifecycle-aware cleanup on reinit/remove.
- Operate with less friction: built-in web UI, JSON backup/restore, CSV onboarding import, and live troubleshooting logs.
- Secure practical defaults: local bind by default, with optional Caddy HTTPS and Basic Auth for managed access.
- Reduce custom glue code: purpose-built bridge from UPS telemetry to automation-ready MQTT data.

## Scope
This repository is intentionally trimmed for standalone deployment.
It includes runtime code and Compose configuration only, and excludes Home Assistant add-on packaging and unrelated development artifacts.

## Included
- `standalone/` Docker Compose runtime
- `ups2mqtt/rootfs/usr/src/app/` application runtime code

## Prerequisites
- Docker Engine (with Docker Compose v2)
- `make`

## Quick start
1. Create your env file at the repository root:
   - `cp standalone/.env.example .env`
2. Edit `.env` and set at minimum:
   - `UPS2MQTT_MQTT_HOST`
   - `UPS2MQTT_MQTT_PORT` (default `1883`)
   - `UPS2MQTT_MQTT_USERNAME` / `UPS2MQTT_MQTT_PASSWORD` if your broker requires auth
   - `UPS2MQTT_PROXY_HOSTNAME` (default `ups2mqtt.local`)
   - `UPS2MQTT_PROXY_USERNAME`
   - `UPS2MQTT_PROXY_PASSWORD_HASH` (replace example hash)
3. Edit `standalone/options.json` for runtime options and device definitions (`config` YAML payload).
4. Start the stack:
   - `make dev-up`
5. Verify:
   - `make dev-ps`
   - `make dev-logs`
   - Proxy UI HTTPS (Basic Auth): `https://ups2mqtt.local:8443/htmx/devices` (startup page)
   - Direct local UI (no auth): `http://127.0.0.1:8099/htmx/devices`

## Optional Caddy Reverse Proxy (HTTPS + Basic Auth)
- Standalone compose includes a lightweight Caddy reverse proxy (`ups2mqtt-caddy`) as the public HTTP/HTTPS entrypoint.
- Caddy enforces HTTP Basic Auth and proxies to `ups2mqtt:8099` on the internal Docker network.
- Compose runs two containers in the same stack/network:
  - `ups2mqtt` (application container, built from `standalone/Dockerfile`)
  - `ups2mqtt-caddy` (reverse proxy sidecar container from `caddy:2-alpine`)
- Default standalone ports:
  - proxy: `UPS2MQTT_PROXY_HTTP_PORT` (default `8080`)
  - proxy TLS: `UPS2MQTT_PROXY_HTTPS_PORT` (default `8443`)
  - direct app bind: `UPS2MQTT_WEB_BIND:UPS2MQTT_WEB_PORT` (default `127.0.0.1:8099`)
- Caddy hostname: `UPS2MQTT_PROXY_HOSTNAME` (default `ups2mqtt.local`)
- Change proxy admin password end-to-end (recommended):
  - `make proxy-set-password PASSWORD='your-new-password'`
- Generate only a password hash with Caddy (optional/manual flow):
  - `docker run --rm -it caddy:2-alpine caddy hash-password`
  - or `make proxy-hash-password PASSWORD='your-new-password'`
- Paste the generated hash into `UPS2MQTT_PROXY_PASSWORD_HASH` in `.env`, replacing each `$` with `$$` for Docker Compose interpolation safety.
- Caddy TLS mode is `tls internal` for local/self-signed certs.
- Browsers will warn unless Caddy's local CA is trusted.
- Caddy stores its local CA and cert assets under the `caddy_data` volume.

## Common commands
- Start/build: `make dev-up`
- Rebuild only: `make dev-build`
- Restart service: `make dev-restart`
- Tail logs: `make dev-logs`
- Stop stack: `make dev-down`
- Restart proxy sidecar only: `make dev-restart SERVICE=caddy`
- Tail proxy sidecar logs: `make dev-logs SERVICE=caddy`
- Generate proxy admin password hash: `make proxy-hash-password PASSWORD='your-new-password'`
- Set proxy admin password end-to-end: `make proxy-set-password PASSWORD='your-new-password'`
- Lock default profiles (`[default]` in name): `make dev-lock`
- Unlock default profiles (`[default]` in name): `make dev-unlock`

## Current Status
- The core runtime reuses proven code from related UPS apps; some UI/display differences are still expected.
- Development is currently focused on this standalone deployment for convenience.
- Direction is toward a Home Assistant add-on/app model (not HACS).
- This standalone instance avoids loading Home Assistant core resources directly and reduces main-loop impact.

## Driver Coverage
- APC Smart UPS (legacy Modbus and legacy SNMP): supported.
- APC PDU: limited support.
- APC SMT devices: supported.
- RFC1628 UPS devices: expected to be supported.
- CyberPower Modbus devices: supported.

## Home Assistant + MQTT Notes
- Tested against Mosquitto MQTT broker in Home Assistant.
- You must create/use an MQTT user for this app.
- Discovery adds/removes entities through MQTT discovery; polling must also be enabled.
- Disabling discovery or deleting a device should remove visible HA values.
- If a deleted ups2mqtt device still appears in HA, remove the device manually in Home Assistant.
- Home Assistant token is not required for normal operation.
- Home Assistant token is used during device reinitialization to remove stale entity data for that device.
- Device list rows include a `Data` modal for read-only preview of currently cached Home Assistant payload data (including empty/not-found states) without publishing MQTT or regenerating discovery.

## Profiles, Polling, and Runtime Behavior
- Global Profiles are functional.
- Local Profiles are work in progress.
- Devices support JSON backup/restore from the Maintenance panel.
  - JSON backup includes devices, profiles, and profile mappings.
  - Backup schema is versioned (`ups2mqtt.device_export` v1).
- CSV is import-only for onboarding from the Maintenance panel.
  - Use `Download CSV Import Template` for the current headers.
  - Legacy CSV without a `Location` column is still accepted.
- Device records include an optional `location` field.
  - Location is editable in Add/Edit Device forms.
  - Location is shown on the Devices table.
  - JSON backup/restore and CSV import preserve location.
- Devices panel filters align to visible table columns: `ID`, `Name`, `Location`, `Host`, and `Profile`.
- Recommended usage: create multiple Global Profiles per scenario (for example `SMT-UIO1-temp-humidity`) and assign one per device.
- Profile sensor selection is now single-toggle: if a sensor is selected it is published to MQTT and discovered by Home Assistant.
- The previous per-sensor `HA visible` option was removed. Existing stored `ha_visible` values are ignored.
- The app currently has 8 polling slots and uses a simple semaphore to share polling resources.
- The polling model is tunable for larger workloads compared with fixed Home Assistant add-on defaults.
- Ignore slow/fast poll settings for now; that concept is not fully wired through and will change.
- `Keep Conn` improves TCP/Modbus efficiency when the NMC keepalive is configured (around 300 seconds).
- The local log buffer is for troubleshooting and does not persist across restarts/reboots.
- The Logs panel shows current in-memory buffer usage (`Logs: N / 2000`) and supports `Clear logs` for buffer-only reset.
- HTMX logs clear route is `POST /htmx/logs/actions/clear`; legacy `POST /htmx/devices/actions/logs/clear` remains supported with a one-time DEBUG deprecation signal.

## Human-Readable Mapping Policy
- Runtime output is now human-readable for mapped status/code fields.
- Integer code fields (for example `output_source`, `battery_status`) publish companion text fields (`output_source_text`, `battery_status_text`).
- Raw bitfield sensors (`*_bf`) are not exposed in profile selection, discovery, or published state.
- Bitfields are decoded into named boolean state fields (for example `ups_online_state`, `ups_on_battery_state`).
- APC Smart-UPS legacy Modbus now decodes `status_word_1`, `status_word_2`, and `status_word_3` into named state flags (fault, power-source, overload, and battery state indicators).
- When a required mapping is missing, the runtime logs a warning and suppresses that unmapped output.

## Security
- The app web interface itself has no authentication/authorization.
- Use the bundled Caddy reverse proxy Basic Auth for any non-local exposure.
- Keep direct app bind local-only (`UPS2MQTT_WEB_BIND=127.0.0.1`) unless you explicitly need otherwise.

## Development
- Project dependencies are managed with `uv` in `ups2mqtt/rootfs/usr/src/app/`.
- Update lockfile after dependency changes:
  - `cd ups2mqtt/rootfs/usr/src/app`
  - `uv lock`
- Runtime settings/devices files now use:
  - `/data/ups2mqtt_settings.yaml`
  - `/data/ups2mqtt_devices.yaml`
- Environment variable namespace is `UPS2MQTT_*`.

## Capability DB Snapshot
- A versioned SQL snapshot can prime all `capability_*` tables in a fresh database.
- Dump snapshot from current DB:
  - `make db-cap-dump`
- Prime DB from snapshot:
  - `make db-cap-prime`
- Maintenance workflow after capability/schema changes:
  - run app startup once to apply DB schema updates and seed changes
  - run `make db-cap-dump` to refresh snapshot SQL
  - commit both code changes and `capabilities/capability_snapshot.sql` together
- Override paths when needed:
  - `make db-cap-dump DB_PATH=standalone/data/ups2mqtt.db CAP_SNAPSHOT=ups2mqtt/rootfs/usr/src/app/capabilities/capability_snapshot.sql`
  - `make db-cap-prime DB_PATH=/tmp/new.db CAP_SNAPSHOT=ups2mqtt/rootfs/usr/src/app/capabilities/capability_snapshot.sql`

## Linting
Run from `ups2mqtt/rootfs/usr/src/app`:
- `./.venv/bin/pytest -q`
- `uv run --group lint ruff check .`
- `uv run --group lint grain check --all`
- `HOME=/tmp uv run --group lint semgrep --config auto --error ups2mqtt`
- `uv run --group lint sqlfluff lint .`
- SQLFluff intentionally ignores capability snapshot dump files via `ups2mqtt/rootfs/usr/src/app/.sqlfluffignore` (`capabilities/capability_snapshot*.sql`) to avoid non-source large-file warnings.

YAML lint from repository root:
- `./ups2mqtt/rootfs/usr/src/app/.venv/bin/yamllint -s --no-warnings standalone/docker-compose.yml ups2mqtt/rootfs/usr/src/app/ups2mqtt`

## Troubleshooting
- If `make dev-up` fails with missing variables, confirm `.env` exists in the repository root.
- Prefer `make` targets over raw `docker compose` commands in this repo. The `make` workflow passes `--env-file .env`; running Compose directly can inject empty `UPS2MQTT_*` values.
- If the service is up but not publishing data, verify MQTT host/port/credentials in `.env` and check `make dev-logs`.
- If startup fails during DB init with `Unsupported table for migration: profiles`, rebuild and restart with `make dev-up` so the latest migration logic is applied to `standalone/data/ups2mqtt.db`.
