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

---

## Why use this?

Many UPS systems expose data via SNMP, or vendor-specific tools, but:

- they don’t integrate cleanly with Home Assistant  
- they lack real-time automation hooks  
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
- Monitor battery health over time  
- Track power quality and load  

---

## Design goals

- Minimal dependencies  
- Works alongside existing UPS tooling  
- MQTT-first (no tight coupling to Home Assistant)  
- Simple to deploy (Docker / add-on)

---

## Compatibility

`ups2mqtt` is designed for mixed UPS environments and supports multiple deployment models.

### Supported UPS / Protocol Coverage
- APC Smart-UPS (legacy Modbus + legacy SNMP): supported
- APC SMT series: supported
- CyberPower Modbus devices: supported
- RFC1628 UPS-MIB devices: broadly compatible (model-specific behavior may vary)
- APC PDU: limited support

### Runtime / Platform
- Python runtime: 3.13+
- Standalone deployment: Docker Compose (included in this repository)
- Home Assistant Community App: supported via Ingress-enabled add-on path (`homeassistant-addon/ups2mqtt/`)

### Home Assistant / MQTT
- MQTT discovery: supported (tested with Home Assistant + Mosquitto)
- Home Assistant token: optional, only needed for stale-entity cleanup flows
- Ingress UI: supported in add-on mode
- Direct web port (add-on): optional troubleshooting mode

### Architecture Support (Home Assistant Add-on)
- `amd64`
- `aarch64`
- `armv7`

### Notes
- Compatibility depends on UPS firmware behavior, MIB/register implementation fidelity, and network quality.
- For unsupported or partially supported models, telemetry coverage can be incomplete even when connectivity succeeds.

---

## Example MQTT payload

    {
      "battery": 87,
      "load": 32,
      "runtime": 1240,
      "status": "ONLINE"
    }

## Runtime observability

The web metrics panel and `/metrics.json` expose scheduler/backpressure telemetry for live tuning:

- Backpressure and adaptive limiter state:
  - `current_limit`, `configured_min`, `configured_max`
  - in-flight vs queued polls
  - wait pressure (`p50/p95/max`) over the rolling 60s window
- Per-driver/source fairness stats:
  - dequeue/completion counts
  - average + p50/p95 wait
  - max queue age
- Per-device timing load averages:
  - `duration_load_avg_ms` and `wait_load_avg_ms` with `1m`, `5m`, `15m` windows

In the metrics UI, device timing columns show the `1m/5m/15m` load averages for duration and wait.
