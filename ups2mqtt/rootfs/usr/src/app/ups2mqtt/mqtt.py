# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import json
import logging
from time import monotonic
from typing import Any

import paho.mqtt.client as mqtt

from .icon_resolver import (
    resolve_device_info,
    resolve_enabled_defaults,
    resolve_icon,
)
from .model import AppConfig, DeviceConfig
from . import pollers

LOG = logging.getLogger("ups2mqtt.mqtt")


def _friendly_name(key: str) -> str:
    return key.replace("_", " ").strip().title()


def _infer_units(key: str) -> tuple[str | None, str | None, str | None]:
    lower = key.lower()
    if "temperature" in lower or lower.endswith("_temp"):
        return "temperature", "°C", "measurement"
    if "humidity" in lower:
        return "humidity", "%", "measurement"
    if "voltage" in lower:
        return "voltage", "V", "measurement"
    if "current" in lower or lower.endswith("_amps"):
        return "current", "A", "measurement"
    if "frequency" in lower:
        return "frequency", "Hz", "measurement"
    if "energy" in lower:
        return "energy", "kWh", "total_increasing"
    if "runtime" in lower or "seconds" in lower:
        return "duration", "min", "measurement"
    if "load" in lower or "charge" in lower or lower.endswith("_percent"):
        return None, "%", "measurement"
    if "power_factor" in lower:
        return "power_factor", None, "measurement"
    if "power" in lower:
        return "power", "W", "measurement"
    return None, None, None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _source_fallback_device_info(source: str) -> tuple[str, str]:
    normalized = source.strip().lower()
    source_map: dict[str, tuple[str, str]] = {
        "apc_modbus_smart": ("APC", "Smart UPS"),
        "apc_modbus_smt": ("APC", "SMT UPS"),
        "apc_modbus_rack_pdu": ("APC", "Rack PDU"),
        "cyberpower_modbus_single_phase": ("CyberPower", "Single Phase UPS"),
        "cyberpower_modbus_three_phase": ("CyberPower", "Three Phase UPS"),
        "ups_snmp_apc_mib": ("APC", "SNMP UPS"),
        "ups_snmp_ups_mib": ("UPS", "SNMP UPS"),
    }
    if normalized in source_map:
        return source_map[normalized]
    if normalized.startswith("apc_modbus"):
        return ("APC", source.replace("_", " ").strip().title())
    if normalized.startswith("cyberpower_modbus"):
        return ("CyberPower", source.replace("_", " ").strip().title())
    if normalized.startswith("ups_snmp"):
        return ("UPS", source.replace("_", " ").strip().title())
    return ("ups2mqtt", source)


class MqttPublisher:
    def __init__(self, config: AppConfig):
        self._config = config
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="ups_unified_mqtt"
        )
        self._bridge_availability_topic = (
            f"{self._config.mqtt_topic_prefix}/bridge/availability"
        )
        if config.mqtt_username:
            self._client.username_pw_set(
                config.mqtt_username, config.mqtt_password or ""
            )
        self._client.will_set(
            self._bridge_availability_topic, payload="offline", qos=1, retain=True
        )
        self._connected = False
        self._last_connect_attempt = 0.0
        self._device_metadata: dict[str, dict[str, str]] = {}
        self._device_state_cache: dict[str, dict[str, Any]] = {}

    def _extract_device_metadata(self, values: dict[str, Any]) -> dict[str, str]:
        def pick(*keys: str) -> str | None:
            for key in keys:
                if key in values:
                    text = _string_or_none(values.get(key))
                    if text:
                        return text
            return None

        metadata: dict[str, str] = {}
        manufacturer = pick("manufacturer", "mfr", "vendor")
        if manufacturer:
            metadata["manufacturer"] = manufacturer
        model = pick("model", "model_name")
        if model:
            metadata["model"] = model
        sw_version = pick("sw_version", "software_version", "firmware", "fw_version")
        if sw_version:
            metadata["sw_version"] = sw_version
        hw_version = pick("hw_version", "hardware_version")
        if hw_version:
            metadata["hw_version"] = hw_version
        serial_number = pick("serial_number", "serial", "sn")
        if serial_number:
            metadata["serial_number"] = serial_number
        configuration_url = pick("configuration_url", "http_url")
        if configuration_url:
            metadata["configuration_url"] = configuration_url
        return metadata

    def _build_device_payload(
        self, device: DeviceConfig, identity: str
    ) -> dict[str, Any]:
        fallback_manufacturer, fallback_model = _source_fallback_device_info(
            device.source
        )
        info: dict[str, Any] = {
            "identifiers": [f"ups_unified_{identity}"],
            "name": device.name or device.id,
            "manufacturer": fallback_manufacturer,
            "model": fallback_model,
            "via_device": "ups_unified_bridge",
            # HA Device Info field for linking to device web UI.
            "configuration_url": f"http://{device.host}/",
        }
        cached = self._device_metadata.get(identity, {})
        if cached.get("manufacturer"):
            info["manufacturer"] = cached["manufacturer"]
        if cached.get("model"):
            info["model"] = cached["model"]
        for key in ("sw_version", "hw_version", "serial_number"):
            if cached.get(key):
                info[key] = cached[key]
        if cached.get("configuration_url"):
            info["configuration_url"] = cached["configuration_url"]
        return info

    def connect(self) -> None:
        if not self._config.mqtt_enabled:
            LOG.info("MQTT disabled by configuration")
            return
        self._attempt_connect(force=True)

    def close(self) -> None:
        if self._connected:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False

    def ensure_connected(self) -> bool:
        return self._attempt_connect()

    def _attempt_connect(self, *, force: bool = False) -> bool:
        if not self._config.mqtt_enabled:
            return False
        now = monotonic()
        if not force and now - self._last_connect_attempt < 5:
            return self._connected
        self._last_connect_attempt = now
        try:
            if self._connected:
                return True
            self._client.connect(
                self._config.mqtt_host, self._config.mqtt_port, keepalive=30
            )
            self._client.loop_start()
            self._connected = True
            self._client.publish(
                self._bridge_availability_topic, payload="online", qos=1, retain=True
            )
            self._publish_bridge_discovery()
            LOG.info(
                "MQTT connected to %s:%s",
                self._config.mqtt_host,
                self._config.mqtt_port,
            )
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            self._connected = False
            LOG.warning(
                "MQTT connect failed to %s:%s: %s",
                self._config.mqtt_host,
                self._config.mqtt_port,
                err,
            )
        return self._connected

    def _publish_bridge_discovery(self) -> bool:
        """Publish discovery message for the bridge itself."""
        unique_id = "ups_unified_bridge"
        config_topic = f"{self._config.mqtt_discovery_prefix}/sensor/{unique_id}/config"
        payload: dict[str, Any] = {
            "name": "ups2mqtt Bridge",
            "unique_id": unique_id,
            "state_topic": self._bridge_availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "icon": "mdi:bridge",
            "device": {
                "identifiers": [unique_id],
                "name": "ups2mqtt Bridge",
                "manufacturer": "ups2mqtt",
                "model": "MQTT Bridge",
            },
        }
        payload_json = json.dumps(payload)
        self._client.publish(config_topic, payload=payload_json, qos=1, retain=True)
        return True

    def publish_discovery(
        self,
        device: DeviceConfig,
        keys: list[str],
        *,
        authoritative_keys: bool = True,
        discovery_visibility: dict[str, bool] | None = None,
    ) -> bool:
        if not self._attempt_connect():
            return False
        if not device.discovery_enabled:
            return False
        state_topic = f"{self._config.mqtt_topic_prefix}/{device.id}/state"
        availability_topic = (
            f"{self._config.mqtt_topic_prefix}/{device.id}/availability"
        )
        identity = device.device_uid or device.id
        device_info = self._build_device_payload(device, identity)

        enabled_defaults = resolve_enabled_defaults(
            device.source,
            keys,
            self._config.apps_dir,
            authoritative=authoritative_keys,
        )

        for key in keys:
            unique_id = f"ups_unified_{identity}_{key}"
            config_topic = (
                f"{self._config.mqtt_discovery_prefix}/sensor/{unique_id}/config"
            )
            device_class, unit, state_class = _infer_units(key)
            payload: dict[str, Any] = {
                "name": _friendly_name(key),
                "unique_id": unique_id,
                "state_topic": state_topic,
                "value_template": "{{ value_json." + key + " }}",
                "availability": [
                    {
                        "topic": self._bridge_availability_topic,
                        "payload_available": "online",
                        "payload_not_available": "offline",
                    },
                    {
                        "topic": availability_topic,
                        "payload_available": "online",
                        "payload_not_available": "offline",
                    },
                ],
                "device": device_info,
            }
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
            if state_class:
                payload["state_class"] = state_class
            # Resolve icon from the device source's external app
            icon = resolve_icon(device.source, key, self._config.apps_dir)
            if icon:
                payload["icon"] = icon
            # Resolve default-enabled state from the device source's external app
            enabled = bool(enabled_defaults.get(key, True))
            if isinstance(discovery_visibility, dict) and key in discovery_visibility:
                enabled = bool(discovery_visibility.get(key, True))
            LOG.debug(
                "Metric %s for %s: enabled_by_default=%s", key, device.source, enabled
            )
            if not enabled:
                payload["enabled_by_default"] = False
            payload["json_attributes_topic"] = state_topic
            payload["json_attributes_template"] = (
                '{{ {"host": value_json._meta.host, "port": value_json._meta.port, '
                '"unit_id": value_json._meta.unit_id, "source": value_json._meta.source} | tojson }}'
            )
            payload_json = json.dumps(payload)
            self._client.publish(config_topic, payload=payload_json, qos=1, retain=True)

        LOG.debug(
            "Publishing availability for %s: topic=%s, payload=online",
            device.id,
            availability_topic,
        )
        self._client.publish(availability_topic, payload="online", qos=1, retain=True)
        return True

    def clear_discovery(self, device: DeviceConfig, keys: list[str]) -> bool:
        if not self._attempt_connect():
            return False
        identity = device.device_uid or device.id
        for key in keys:
            unique_id = f"ups_unified_{identity}_{key}"
            config_topic = (
                f"{self._config.mqtt_discovery_prefix}/sensor/{unique_id}/config"
            )
            self._client.publish(config_topic, payload="", qos=1, retain=True)
        return True

    def clear_legacy_discovery(self, device_id: str, keys: list[str]) -> bool:
        if not self._attempt_connect():
            return False
        for key in keys:
            unique_id = f"ups_unified_{device_id}_{key}"
            config_topic = (
                f"{self._config.mqtt_discovery_prefix}/sensor/{unique_id}/config"
            )
            self._client.publish(config_topic, payload="", qos=1, retain=True)
        return True

    def publish_state(
        self,
        device: DeviceConfig,
        values: dict[str, Any],
        *,
        discovery_keys: list[str] | None = None,
        discovery_visibility: dict[str, bool] | None = None,
    ) -> bool:
        if not self._attempt_connect():
            return False

        identity = device.device_uid or device.id

        # Extract metadata from current poll values
        extracted = self._extract_device_metadata(values)
        from_contract = resolve_device_info(
            device.source, values, self._config.apps_dir
        )

        # Get runtime metadata from independent cache (populated separately from sensor polls)
        # This allows device identity to be available even when metadata sensors are not enabled
        from_runtime_cache = pollers.get_runtime_metadata(device)

        # Merge all metadata sources (runtime cache takes precedence over poll values)
        merged = dict(extracted)
        merged.update(from_contract)
        merged.update(from_runtime_cache)

        previous = self._device_metadata.get(identity, {})
        metadata_changed = merged != previous
        if metadata_changed:
            self._device_metadata[identity] = merged

        state_topic = f"{self._config.mqtt_topic_prefix}/{device.id}/state"
        previous_state = self._device_state_cache.get(identity, {})
        payload = dict(previous_state)
        payload.update(values)
        self._device_state_cache[identity] = dict(payload)
        payload["_meta"] = {
            "host": device.host,
            "port": device.port,
            "unit_id": device.unit_id,
            "source": device.source,
        }
        LOG.debug(
            "Publishing state for %s: topic=%s, identity=%s, keys=%s",
            device.id,
            state_topic,
            identity,
            list(payload.keys())[:10],
        )
        self._client.publish(
            state_topic,
            payload=json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=True,
        )

        # Republish discovery when identifying metadata changes so HA Device Info updates.
        if metadata_changed and device.discovery_enabled:
            try:
                # Evaluate enabled-by-default against the full profile key set, not the current payload subset, to avoid false "all metrics disabled" warnings during partial discovery republish.
                republish_keys = sorted(set(discovery_keys or values.keys()))
                self.publish_discovery(
                    device,
                    republish_keys,
                    authoritative_keys=bool(discovery_keys),
                    discovery_visibility=discovery_visibility,
                )
            except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                LOG.debug("Discovery republish on metadata change failed: %s", err)
        return True

    def publish_unavailable(self, device: DeviceConfig) -> bool:
        if not self._attempt_connect():
            return False
        availability_topic = (
            f"{self._config.mqtt_topic_prefix}/{device.id}/availability"
        )
        self._client.publish(availability_topic, payload="offline", qos=1, retain=True)
        return True
