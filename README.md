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
   - UI: `http://localhost:8099`

## Common commands
- Start/build: `make dev-up`
- Rebuild only: `make dev-build`
- Restart service: `make dev-restart`
- Tail logs: `make dev-logs`
- Stop stack: `make dev-down`

## Troubleshooting
- If `make dev-up` fails with missing variables, confirm `.env` exists in the repository root.
- If the service is up but not publishing data, verify MQTT host/port/credentials in `.env` and check `make dev-logs`.
