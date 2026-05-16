# ups2mqtt Home Assistant Add-on

`ups2mqtt` is a Home Assistant add-on/app that publishes UPS telemetry to MQTT and exposes entities through MQTT discovery. No custom Home Assistant integration is required.

## Install

1. Add this repository in Home Assistant:
   - `https://github.com/aburow/ups2mqtt-db`
2. Install the `ups2mqtt` add-on.
3. Start the add-on.

## MQTT broker requirements

- A working MQTT broker in Home Assistant (for example, Mosquitto add-on).
- Broker host/port and credentials configured in add-on options when required.
- The add-on requires MQTT service availability (`services: [mqtt:need]`).

## Configuration

Primary options (from `config.yaml` schema):

- `mqtt_enabled`, `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password`
- `mqtt_discovery_prefix` (default `homeassistant`)
- `mqtt_topic_prefix` (default `ups2mqtt`)
- `config` (device list payload)
- `poll_interval`, `poll_timeout`, `max_concurrent_polls`
- `web_enabled`, `web_port`, `metrics_port`

Runtime options are read from `/data/options.json` and prepared into `/data/ups2mqtt_internal_options.json`.

## Entity creation and discovery behavior

- Discovery topics are published under:
  - `<discovery_prefix>/sensor/<unique_id>/config`
- State topics are published under:
  - `<topic_prefix>/<device_id>/state`
- Availability topics are published under:
  - `<topic_prefix>/<device_id>/availability`
  - `<topic_prefix>/bridge/availability`
- Discovery payloads are retained by design so Home Assistant can restore entities after restart.
- Legacy discovery namespaces are actively cleaned up to prevent duplicate/stale entities.

## Troubleshooting

- Confirm MQTT connectivity and credentials first.
- Check add-on logs for connection/authentication errors.
- Validate discovery prefix/topic prefix consistency with your broker and HA MQTT integration.
- Use direct ports (`8099`, `8100`) only when needed for troubleshooting.

## Removal and topology changes

- When devices are removed or profiles change, stale discovery topics are cleared.
- Availability is set to `offline` when polling is disabled or device tasks are removed.
- One-time legacy discovery cleanup marker is stored in `/data/.discovery_v2_migrated`.

## Known limitations

- Coverage depends on protocol/model support (NUT, APCUPSD, SNMP, Modbus profile availability).
- Optional telemetry exporters (Prometheus/Influx) are secondary to MQTT/HA discovery flow.

## Standalone vs add-on behavior

- Add-on mode: Supervisor-managed options in `/data/options.json`, ingress-aware UI.
- Standalone mode: Docker Compose flow under `standalone/` with its own options mount and runtime wiring.
