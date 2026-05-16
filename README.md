<div align="center">

# ⚡ ups2mqtt

<img src="./docs/assets/logo.png" width="120"/>

**UPS telemetry bridge for MQTT and Home Assistant**

Expose UPS metrics (battery, load, runtime, status) via MQTT and integrate reliably into Home Assistant.

---

[![GitHub Release](https://img.shields.io/github/v/release/aburow/ups2mqtt-db?label=stable&color=green)](https://github.com/aburow/ups2mqtt-db/releases)
[![GitHub Issues](https://img.shields.io/github/issues/aburow/ups2mqtt-db)](https://github.com/aburow/ups2mqtt-db/issues)
[![GitHub Stars](https://img.shields.io/github/stars/aburow/ups2mqtt-db)](https://github.com/aburow/ups2mqtt-db/stargazers)
[![License](https://img.shields.io/github/license/aburow/ups2mqtt-db)](LICENSE)

---

[📖 Documentation](https://github.com/aburow/ups2mqtt-db#readme) •
[🐛 Report Bug](https://github.com/aburow/ups2mqtt-db/issues/new?labels=bug) •
[💡 Request Feature](https://github.com/aburow/ups2mqtt-db/issues/new?labels=enhancement)

---

[![Add Repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https://github.com/aburow/ups2mqtt-db)

</div>

## What is ups2mqtt?

**ups2mqtt** connects UPS devices to Home Assistant by publishing telemetry over MQTT.

It collects key metrics such as:
- battery level
- load percentage
- runtime remaining
- input/output voltage
- UPS status (online, on battery, charging)
... and much more for our vendor specific drivers

and makes them available for:
- Home Assistant sensors
- automations (e.g. shutdown triggers, alerts)
- dashboards

With optional telemetry feeds for:
- Prometheus
- Influx V3

---

## Why use this?

Other UPS systems expose data via SNMP, or vendor-specific tools, but:

- they don’t integrate cleanly with Home Assistant
- they are vendor specific
- or they require heavyweight software stacks

**ups2mqtt** provides a lightweight bridge that:
- publishes UPS data to MQTT in a structured format  
- works natively with Home Assistant’s MQTT integration  
- keeps your setup simple and observable  

---

## How it fits into Home Assistant

ups2mqtt acts as a data source:

UPS → ups2mqtt → MQTT → Home Assistant

Once connected, you can:

- create sensors via MQTT discovery or manual config  
- trigger automations (e.g. power outage alerts)  
- visualize UPS health in dashboards  

📖 See: https://www.home-assistant.io/integrations/mqtt/

---

## Typical use cases

- Notify when power is lost or restored  
- Safely shut down servers during extended outages  
- Monitor battery health over time (not available with all UPS types)
- Track power quality and load (not available with all UPS types)

---

## Design goals

- Minimal dependencies  
- Works alongside existing UPS tooling
- Use existing tooling permanently or as a migration path
- MQTT-first (no tight coupling to Home Assistant)  
- Simple to deploy (Docker / add-on)

---

## Project rules

- Changelog history is append-only: when updating changelog files, preserve all existing historical entries and do not wipe prior data.

---

## Compatibility

`ups2mqtt` is designed for mixed UPS environments and supports multiple deployment models.

### Supported UPS / Protocol Coverage
- **NUT (network upsd)**: supported, including multiple UPS names behind one NUT host:port.
- **APCUPSD (NIS/network)**: supported.
- **SNMP (UPS-MIB / APC / CyberPower profiles)**: supported (model/MIB coverage varies by device).
- **Modbus (APC/CyberPower profile-based support)**: supported.
- **APC PDU**: supported where protocol/profile mappings exist; field coverage depends on device/MIB/register implementation.

### Runtime / Platform
- Python runtime: 3.13+
- Standalone deployment: Docker Compose (included in this repository)
- Home Assistant Community App: supported via Ingress-enabled add-on path (`homeassistant-addon/ups2mqtt/`)

### Home Assistant / MQTT
- MQTT discovery: supported
- No custom Home Assistant integration is required; entities are created directly via MQTT discovery.
- Home Assistant token: optional, used for stale-entity cleanup flows
- Ingress UI: supported in add-on mode
- Direct web port (add-on): optional troubleshooting mode

### Optional Telemetry Outputs
- **Prometheus scrape endpoint**: supported (selected numeric values only)
- Metrics listener: `:8100`
- Paths: `/metrics/prometheus` and `/metrics`
- **InfluxDB v3 line protocol export**: supported (optional, disabled by default)
- Endpoint used: `/api/v3/write_lp`
- Export scope: selected numeric values only
- Non-blocking design: bounded queue + background worker; exporter failures do not block polling/MQTT/HA flows

### Architecture Support (Home Assistant Add-on)
- `amd64`
- `aarch64`
- `armv7`

### Notes
- Compatibility depends on UPS firmware behavior, MIB/register implementation fidelity, and network quality.
- Telemetry coverage can be partial on unsupported or vendor-variant models even when connectivity succeeds.
- MQTT/Home Assistant remain the primary output path; Prometheus/Influx are optional.

---

## Example MQTT payload

    {
      "battery": 87,
      "load": 32,
      "runtime": 1240,
      "status": "ONLINE"
    }

## MQTT discovery key normalization

For selected raw NUT variables that contain dotted keys (for example `battery.voltage`), ups2mqtt keeps the raw key in the MQTT state payload and uses bracket-safe templates in discovery:

- state payload key: `battery.voltage`
- discovery value template: `{{ value_json['battery.voltage'] }}`

Home Assistant discovery identifiers/topics are normalized to HA-safe tokens (for example `battery_voltage`) to avoid entity-registration issues with dotted identifiers.

## Runtime observability

The web metrics panel and `/metrics.json` expose scheduler/backpressure telemetry for live tuning:

- Backpressure and fixed limiter state:
  - preferred key: `backpressure.concurrency_limiter` (`current_limit`, `configured_min`, `configured_max`)
  - deprecated compatibility alias: `backpressure.adaptive_concurrency` (same object as `concurrency_limiter`)
  - in-flight vs queued polls
  - wait pressure (`p50/p95/max`) over the rolling 60s window
- Per-driver/source fairness stats:
  - dequeue/completion counts
  - average + p50/p95 wait
  - max queue age
- Per-device timing load averages:
  - `duration_load_avg_ms` and `wait_load_avg_ms` with `1m`, `5m`, `15m` windows
- Slot scheduler comparison fields:
  - `missed_capacity_count`
  - `missed_overlap_count`
  - `last_success_age_seconds`
  - `polls_started_per_second`, `polls_completed_per_second`, `timeout_rate`, `event_loop_lag_ms`

In the metrics UI, device timing columns show the `1m/5m/15m` load averages for duration and wait.

## Polling behavior

The default poll interval is centralized at 15 seconds. Runtime configuration may set a higher global `poll_interval`, but per-device and per-profile poll intervals are clamped so they cannot run faster than the configured global/default minimum. The built-in `fast` poll group also follows this minimum and is not user-overridden through profile import/edit flows.

Device polling is scheduled into round-robin time slots by source so large banks of devices do not all start at once. If a slot cannot acquire capacity, the poll is skipped for that slot and counted as `missed_capacity_count`; if a prior poll still overlaps the next slot, it is counted as `missed_overlap_count`.

SNMP polling batches single-OID reads and multi-candidate fallback reads into one SNMP GET per device cycle. APC, CyberPower, and UPS-MIB metadata refreshes also batch their OID lookups, and APC external probe discovery/merge paths batch probe candidates instead of trying OIDs sequentially. This avoids constructing a separate SNMP engine/transport path for every metric or fallback candidate and keeps the simulator and real devices closer to the intended 15-second cadence.

The metrics panel includes a top-level `Clear All Errors` action that clears only the displayed `last_error` text for every metrics row. It does not reset poll counters, timing history, missed-slot counters, or success/failure totals.

## Latest release (v1.2.10)

- Added HAOS pre-release validation automation with Proxmox snapshot/rollback safety.
- Validated staged local add-on install/start/restart behavior in Supervisor.
- Added runtime checks for real MQTT publish/Home Assistant discovery evidence from add-on logs.
- Preserved production GHCR image metadata while supporting local-build staging for validation.

## Home Assistant test environment

This repository includes Make targets for a disposable local Home Assistant Container + MQTT broker environment. This environment is useful for local MQTT discovery and UI smoke testing, but it is not Supervisor-capable and does not provide the Home Assistant Add-on Store, Supervisor-managed add-on lifecycle, or Supervisor AppArmor validation.

Final add-on acceptance still requires a real Home Assistant OS, Home Assistant Supervised, or disposable Supervisor-capable environment.

Create a local environment file first:

```sh
cp .env.ha-test.example .env.ha-test
```

Default access:

- Home Assistant: `http://localhost:8123`
- MQTT broker: `localhost:1883`

Ports and bind addresses can be changed in `.env.ha-test`.

Available targets:

- `make ha-test-start`: start the disposable Home Assistant Container + MQTT environment.
- `make ha-test-stop`: stop the environment without removing its compose-scoped volumes.
- `make ha-test-rebuild`: rebuild and recreate the environment containers.
- `make ha-test-logs`: follow logs for both test-environment containers.
- `make ha-test-status`: show container status and access information.
- `make ha-test-clean`: remove only the containers, network, and volumes created for the `ups2mqtt-ha-test` compose project.

## Pre-release HAOS SSH override

The pre-release Home Assistant OS validation targets use `PRE_RELEASE_SSH_CMD`, which defaults to `ssh`. Override it only when the runner's global SSH client configuration is broken or when validation must use a clean wrapper. The value may be set in the ignored `.env.pre-release` file or passed on the `make` command line:

```sh
make PRE_RELEASE_SSH_CMD=/path/to/ssh-wrapper pre-release-haos-smoke
```

For example, a local runner can bypass global SSH client config with:

```sh
PRE_RELEASE_SSH_CMD='ssh -F /dev/null'
```

The override still receives the existing key, port, user, host, and remote `ha` CLI arguments from the Make targets. Do not put private key contents or credentials in the command value.

## Pre-release runtime validation command

`make pre-release-run` is smoke-only unless `PRE_RELEASE_TEST_CMD` is set. In smoke-only mode it verifies:

- snapshot creation and verification
- HAOS smoke access (`ha core info` and `ha supervisor info`)
- rollback execution and verification

For Supervisor add-on runtime validation, set:

```sh
PRE_RELEASE_TEST_CMD='./scripts/pre-release-haos-addon-test.sh'
```

The runtime script exercises add-on repository refresh, add-on install/start, AppArmor/log checks, options handling, MQTT discovery/log evidence checks, and add-on restart recovery in HAOS Supervisor.

Recommended one-time setup:

```sh
cp .env.pre-release.example .env.pre-release
```

Release readiness should be treated as incomplete until the runtime validation command passes.

## V2 rollout gate

- Use the V2 release checklist: [docs/V2_READINESS.md](docs/V2_READINESS.md)
