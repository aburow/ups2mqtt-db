<div align="center">

# ⚡ ups2mqtt

<img src="./docs/assets/logo.png" width="120"/>

**UPS telemetry bridge for MQTT and Home Assistant**

Expose UPS metrics (battery, load, runtime, status) via MQTT and integrate seamlessly into Home Assistant.

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

## Example MQTT payload

    {
      "battery": 87,
      "load": 32,
      "runtime": 1240,
      "status": "ONLINE"
    }

