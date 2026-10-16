# ups2mqtt Requirements

## Purpose and scope

`ups2mqtt` collects UPS telemetry and publishes it to MQTT.  Home Assistant
consumes the MQTT discovery and state topics; no custom Home Assistant
integration is required.  The primary delivery targets are the Home Assistant
add-on and the standalone Docker Compose deployment.

This document records the current product contract from the source tree.  A
new change should update this document when it changes observable behaviour.

## Functional requirements

### UPS connectivity

- The service must support NUT (network `upsd`), APCUPSD NIS, SNMP
  (UPS-MIB/APC/CyberPower profiles), and profile-based APC/CyberPower Modbus.
- It must support more than one configured device, including multiple NUT UPS
  names at one NUT host and port.
- Device and profile configuration must be persisted, editable through the web
  UI, and exportable/importable as a versioned backup.
- Removed devices or changed profiles must clear obsolete discovery topics and
  mark their availability offline.

### MQTT and Home Assistant

- MQTT is the primary output and must publish device state, device
  availability, bridge availability, and retained Home Assistant discovery
  configuration.
- Discovery topics must use
  `<discovery_prefix>/sensor/<unique_id>/config`; state topics must use
  `<topic_prefix>/<device_id>/state`.
- Raw NUT keys may remain dotted in state JSON, while discovery identifiers
  and templates must use Home Assistant-safe key handling.
- Publish only the raw NUT variables selected by the device profile, preserving
  their original dotted keys in state data and using safe bracket access in
  Home Assistant templates.
- MQTT connection/authentication failures must be observable in the service
  logs and web UI.
- A Home Assistant API token is optional and may be used only for stale-entity
  cleanup; MQTT discovery must work without it.

### Polling and resilience

- The default minimum poll interval is 15 seconds.  Per-device, per-profile,
  and `fast` group intervals must not run faster than the configured global
  minimum.
- Polls must be scheduled fairly by source and bounded by the configured
  concurrency limit.  Capacity and overlap skips must be counted rather than
  causing unbounded work.
- A failed optional telemetry export must not block polling, MQTT publication,
  or Home Assistant discovery.
- Persistent protocol sessions must be isolated by device identity; NUT
  sessions must also distinguish UPS names at the same host and port. A stale
  NUT session may be retried once, then failures must surface normally.
- Influx export must accept only finite numeric values (not booleans), drop
  work rather than block when its bounded queue is full, and isolate worker or
  enqueue failures from the main polling path.
- Modbus optimizer V2 must retain a usable legacy/V1 path and provide a
  dry-run and rollback mechanism while its dispatch behaviour is being
  validated.
- If V2 dispatch fails, polling must fall back to V1 and latch that device to
  V1 for subsequent polls.

### Web UI and observability

- The service must provide an add-on ingress-aware web UI for managing
  devices, profiles, logs, configuration, maintenance, and metrics.
- It must expose `/metrics.json` and Prometheus-compatible `/metrics` and
  `/metrics/prometheus` endpoints.  Prometheus output must include selected
  numeric telemetry only and must not expose connection credentials or host
  details.
- Prometheus output must omit strings and booleans, escape label values, and
  must not include host, port, UPS name, or credential labels.
- InfluxDB v3 line-protocol export is optional, disabled by default, and uses
  a bounded background queue.
- Influx API tokens must be redacted from sanitized configuration and logs.

### Profile editing

- Profile edits and rediscovery must preserve unknown vendor-specific fields
  and existing user selections unless the user explicitly changes them.

### Deployment and configuration

- The Home Assistant add-on must support `amd64` and `aarch64`, require an
  MQTT service, run with AppArmor enabled, and default to ingress on port
  8099.  Its direct web and metrics ports are troubleshooting interfaces.
- Add-on configuration must validate MQTT, polling, web, Home Assistant,
  Influx, and device-list options according to
  `homeassistant-addon/ups2mqtt/config.yaml`.
- Standalone deployment must be available through
  `standalone/docker-compose.yml` and accept its runtime configuration through
  environment variables and `standalone/options.json`.

## Engineering and release requirements

- `homeassistant-addon/ups2mqtt/app/ups2mqtt/` is the canonical application
  tree.  Its mirrored runtime tree at `ups2mqtt/rootfs/usr/src/app/ups2mqtt/`
  must remain identical; verify with `make runtime-check` after application
  changes and synchronize with `make runtime-sync` when needed.
- The supported Python version is 3.13 or later.
- Tests must cover non-trivial behaviour, especially protocol parsing,
  configuration validation, polling plans, MQTT discovery, telemetry output,
  and web backup/import behaviour.
- Run lint and security checks through the project environment: `uv run grain`
  and `uv run semgrep`.  A Grain error or Semgrep finding blocks release unless
  explicitly waived.
- Changelog files are append-only; historic release entries must be preserved.
- Add-on release acceptance requires a Supervisor-capable Home Assistant
  environment.  The repository's container test environment is useful for UI
  and MQTT smoke tests but is not a substitute for that validation.

## Acceptance evidence

A release is ready only when the relevant automated tests pass, the two
runtime trees are synchronized, required lint/security checks pass or have an
explicit waiver, and add-on changes have been validated in a
Supervisor-capable environment.  For Modbus V2 dispatch changes, also complete
the evidence checklist in `docs/V2_READINESS.md`.

## Source of truth

- Product behaviour: `README.md` and `homeassistant-addon/ups2mqtt/DOCS.md`
- Add-on options and platform contract:
  `homeassistant-addon/ups2mqtt/config.yaml`
- Runtime synchronization: `scripts/sync_runtime_tree.sh` and
  `scripts/check_runtime_tree_sync.sh`
- Modbus V2 release gate: `docs/V2_READINESS.md`
