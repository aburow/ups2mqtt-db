# ups2mqtt-standalone

Standalone Docker Compose deployment for `ups2mqtt`.

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
   - `UPS_UNIFIED_MQTT_HOST`
   - `UPS_UNIFIED_MQTT_PORT` (default `1883`)
   - `UPS_UNIFIED_MQTT_USERNAME` / `UPS_UNIFIED_MQTT_PASSWORD` if your broker requires auth
3. Edit `standalone/options.json` for runtime options and device definitions (`config` YAML payload).
4. Start the stack:
   - `make dev-up`
5. Verify:
   - `make dev-ps`
   - `make dev-logs`
   - UI: `http://localhost:8099/htmx/devices` (startup page)

## Common commands
- Start/build: `make dev-up`
- Rebuild only: `make dev-build`
- Restart service: `make dev-restart`
- Tail logs: `make dev-logs`
- Stop stack: `make dev-down`

## Development
- Project dependencies are managed with `uv` in `ups2mqtt/rootfs/usr/src/app/`.
- Update lockfile after dependency changes:
  - `cd ups2mqtt/rootfs/usr/src/app`
  - `uv lock`

## Linting
Run from `ups2mqtt/rootfs/usr/src/app`:
- `uv run --group lint ruff check .`
- `uv run --group lint grain check --all`
- `HOME=/tmp uv run --group lint semgrep --config auto --error ups2mqtt`
- `uv run --group lint sqlfluff lint .`

YAML lint from repository root:
- `./ups2mqtt/rootfs/usr/src/app/.venv/bin/yamllint -s --no-warnings standalone/docker-compose.yml ups2mqtt/rootfs/usr/src/app/ups2mqtt`

## Troubleshooting
- If `make dev-up` fails with missing variables, confirm `.env` exists in the repository root.
- Prefer `make` targets over raw `docker compose` commands in this repo. The `make` workflow passes `--env-file .env`; running Compose directly can inject empty `UPS_UNIFIED_*` values.
- If the service is up but not publishing data, verify MQTT host/port/credentials in `.env` and check `make dev-logs`.
- If startup fails during DB init with `Unsupported table for migration: profiles`, rebuild and restart with `make dev-up` so the latest migration logic is applied to `standalone/data/ups2mqtt.db`.
