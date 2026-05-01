# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .model import AppConfig, DeviceConfig

RUNTIME_DEVICES_PATH = "/data/ups2mqtt_devices.yaml"
RUNTIME_SETTINGS_PATH = "/data/ups2mqtt_settings.yaml"
STANDALONE_OPTIONS_PATH = "/usr/src/app/options.json"


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _env_or_default(name: str, default: Any) -> Any:
    value = os.environ.get(name)
    if value is None:
        return default
    return value


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_web_base_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "/"
    if not text.startswith("/"):
        text = f"/{text}"
    text = text.rstrip("/")
    return text or "/"


def _resolve_runtime_devices_path() -> str:
    return os.environ.get("UPS2MQTT_RUNTIME_DEVICES_PATH", RUNTIME_DEVICES_PATH)


def _resolve_runtime_settings_path() -> str:
    return os.environ.get("UPS2MQTT_RUNTIME_SETTINGS_PATH", RUNTIME_SETTINGS_PATH)


def _load_raw_options(options_path: str | None) -> dict[str, Any]:
    candidates: list[Path] = []
    env_path = os.environ.get("UPS2MQTT_OPTIONS_PATH")
    if env_path:
        candidates.append(Path(env_path))
    if options_path:
        candidates.append(Path(options_path))
    candidates.append(Path("/data/options.json"))
    candidates.append(Path(STANDALONE_OPTIONS_PATH))

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return {}


def _parse_device(item: dict[str, Any]) -> DeviceConfig:
    device_id = str(item["id"]).strip()
    source = str(item["source"]).strip()
    host = str(item["host"]).strip()
    if not device_id:
        raise ValueError("Device id is required")
    if not source:
        raise ValueError(f"Device {device_id}: source is required")
    if not host:
        raise ValueError(f"Device {device_id}: host is required")
    default_port = 3493 if source.startswith("nut") else 502
    local_profile_payload: dict[str, Any] | None = None
    raw_local_payload = item.get("local_profile_payload")
    if isinstance(raw_local_payload, dict):
        local_profile_payload = {
            str(key): value for key, value in raw_local_payload.items()
        }
    local_selected_sensors: list[str] | None = None
    raw_local_selected = item.get("local_selected_sensors")
    if isinstance(raw_local_selected, list):
        local_selected_sensors = [
            str(value) for value in raw_local_selected if str(value)
        ]
    local_sensor_preferences: dict[str, dict[str, bool]] | None = None
    raw_local_preferences = item.get("local_sensor_preferences")
    if isinstance(raw_local_preferences, dict):
        local_sensor_preferences = {}
        for key, raw in raw_local_preferences.items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                continue
            local_sensor_preferences[key] = {
                "mqtt_enabled": bool(raw.get("mqtt_enabled", True)),
            }
    return DeviceConfig(
        id=device_id,
        source=source,
        host=host,
        port=int(item.get("port", default_port)),
        snmp_port=int(item.get("snmp_port", 161)),
        unit_id=int(item.get("unit_id", 1)),
        snmp_community=str(item.get("snmp_community", "public")),
        poll_interval=int(item["poll_interval"])
        if item.get("poll_interval") is not None
        else None,
        name=_clean_optional(item.get("name")),
        location=_clean_optional(item.get("location")),
        debug_logging=bool(item.get("debug_logging", False)),
        keep_connection_open=bool(item.get("keep_connection_open", False)),
        device_uid=str(item.get("device_uid", "")).strip() or str(uuid4()),
        discovery_enabled=bool(item.get("discovery_enabled", True)),
        polling_enabled=bool(item.get("polling_enabled", True)),
        profile_uid=str(item.get("profile_uid", "")).strip(),
        profile_mode=str(item.get("profile_mode", "local")).strip() or "local",
        local_profile_payload=local_profile_payload,
        local_selected_sensors=local_selected_sensors,
        local_sensor_preferences=local_sensor_preferences,
        enable_extended_fields=bool(item.get("enable_extended_fields", False)),
    )


def _parse_devices(parsed: dict[str, Any]) -> list[DeviceConfig]:
    devices: list[DeviceConfig] = []
    for item in parsed.get("devices", []):
        if not isinstance(item, dict):
            continue
        devices.append(_parse_device(item))
    return devices


def _device_to_dict(device: DeviceConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": device.id,
        "device_uid": device.device_uid,
        "source": device.source,
        "host": device.host,
        "port": device.port,
        "snmp_port": device.snmp_port,
        "unit_id": device.unit_id,
        "snmp_community": device.snmp_community,
    }
    if device.poll_interval is not None:
        payload["poll_interval"] = device.poll_interval
    if device.name:
        payload["name"] = device.name
    if device.location:
        payload["location"] = device.location
    if device.debug_logging:
        payload["debug_logging"] = True
    if device.keep_connection_open:
        payload["keep_connection_open"] = True
    if not device.discovery_enabled:
        payload["discovery_enabled"] = False
    if not device.polling_enabled:
        payload["polling_enabled"] = False
    if device.profile_uid:
        payload["profile_uid"] = device.profile_uid
    if device.profile_mode and device.profile_mode != "local":
        payload["profile_mode"] = device.profile_mode
    if isinstance(device.local_profile_payload, dict):
        payload["local_profile_payload"] = dict(device.local_profile_payload)
    if device.local_selected_sensors is not None:
        payload["local_selected_sensors"] = [
            str(item) for item in device.local_selected_sensors if str(item)
        ]
    if isinstance(device.local_sensor_preferences, dict):
        payload["local_sensor_preferences"] = {
            str(key): {
                "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
            }
            for key, values in device.local_sensor_preferences.items()
            if isinstance(key, str) and isinstance(values, dict)
        }
    return payload


def load_runtime_devices(path: str | None = None) -> list[DeviceConfig]:
    resolved_path = path or _resolve_runtime_devices_path()
    runtime_path = Path(resolved_path)
    if not runtime_path.exists():
        return []
    parsed = yaml.safe_load(runtime_path.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        return []
    return _parse_devices(parsed)


def save_runtime_devices(devices: list[DeviceConfig], path: str | None = None) -> None:
    resolved_path = path or _resolve_runtime_devices_path()
    runtime_path = Path(resolved_path)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"devices": [_device_to_dict(device) for device in devices]}
    runtime_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def load_runtime_settings(path: str | None = None) -> dict[str, Any]:
    resolved_path = path or _resolve_runtime_settings_path()
    runtime_path = Path(resolved_path)
    if not runtime_path.exists():
        return {}
    parsed = yaml.safe_load(runtime_path.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def save_runtime_settings(settings: dict[str, Any], path: str | None = None) -> None:
    resolved_path = path or _resolve_runtime_settings_path()
    runtime_path = Path(resolved_path)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(
        yaml.safe_dump(settings, sort_keys=False),
        encoding="utf-8",
    )


def load_config(options_path: str | None = None) -> AppConfig:
    raw_options = _load_raw_options(options_path)
    embedded_yaml = raw_options.get("config", "")
    parsed = yaml.safe_load(embedded_yaml) if embedded_yaml else {}
    if not isinstance(parsed, dict):
        parsed = {}
    devices = _parse_devices(parsed)
    runtime_settings = load_runtime_settings()

    runtime_devices = load_runtime_devices()
    if runtime_devices:
        devices = runtime_devices

    mqtt_host = str(
        _env_or_default(
            "UPS2MQTT_MQTT_HOST", raw_options.get("mqtt_host", "core-mosquitto")
        )
    ).strip()
    mqtt_port_raw = _env_or_default(
        "UPS2MQTT_MQTT_PORT", raw_options.get("mqtt_port", 1883)
    )
    mqtt_username = _clean_optional(
        _env_or_default("UPS2MQTT_MQTT_USERNAME", raw_options.get("mqtt_username"))
    )
    mqtt_password = _clean_optional(
        _env_or_default("UPS2MQTT_MQTT_PASSWORD", raw_options.get("mqtt_password"))
    )
    raw_ha_bridge_enabled = runtime_settings.get(
        "ha_bridge_enabled",
        raw_options.get("ha_bridge_enabled", False),
    )
    ha_bridge_enabled = _coerce_bool(
        _env_or_default("UPS2MQTT_HA_BRIDGE_ENABLED", raw_ha_bridge_enabled),
        default=False,
    )
    max_concurrent_polls = max(1, int(raw_options.get("max_concurrent_polls", 8)))
    adaptive_concurrency_enabled = _coerce_bool(
        _env_or_default(
            "UPS2MQTT_ADAPTIVE_CONCURRENCY_ENABLED",
            raw_options.get("adaptive_concurrency_enabled", False),
        ),
        default=False,
    )
    adaptive_concurrency_min = max(
        1,
        int(
            _env_or_default(
                "UPS2MQTT_ADAPTIVE_CONCURRENCY_MIN",
                raw_options.get("adaptive_concurrency_min", max_concurrent_polls),
            )
        ),
    )
    adaptive_concurrency_max = max(
        adaptive_concurrency_min,
        int(
            _env_or_default(
                "UPS2MQTT_ADAPTIVE_CONCURRENCY_MAX",
                raw_options.get(
                    "adaptive_concurrency_max",
                    max(max_concurrent_polls, adaptive_concurrency_min),
                ),
            )
        ),
    )
    adaptive_concurrency_window_seconds = max(
        10,
        int(
            _env_or_default(
                "UPS2MQTT_ADAPTIVE_CONCURRENCY_WINDOW_SECONDS",
                raw_options.get("adaptive_concurrency_window_seconds", 60),
            )
        ),
    )
    adaptive_concurrency_target_p95_wait_ms = max(
        0,
        int(
            _env_or_default(
                "UPS2MQTT_ADAPTIVE_CONCURRENCY_TARGET_P95_WAIT_MS",
                raw_options.get("adaptive_concurrency_target_p95_wait_ms", 1000),
            )
        ),
    )

    return AppConfig(
        mqtt_enabled=bool(raw_options.get("mqtt_enabled", True)),
        mqtt_host=mqtt_host or "core-mosquitto",
        mqtt_port=int(mqtt_port_raw),
        mqtt_username=mqtt_username,
        mqtt_password=mqtt_password,
        mqtt_discovery_prefix=str(
            raw_options.get("mqtt_discovery_prefix", "homeassistant")
        ),
        mqtt_topic_prefix=str(raw_options.get("mqtt_topic_prefix", "ups2mqtt")),
        poll_interval=max(1, int(raw_options.get("poll_interval", 10))),
        poll_timeout=max(2, int(raw_options.get("poll_timeout", 15))),
        max_concurrent_polls=max_concurrent_polls,
        adaptive_concurrency_enabled=adaptive_concurrency_enabled,
        adaptive_concurrency_min=adaptive_concurrency_min,
        adaptive_concurrency_max=adaptive_concurrency_max,
        adaptive_concurrency_window_seconds=adaptive_concurrency_window_seconds,
        adaptive_concurrency_target_p95_wait_ms=adaptive_concurrency_target_p95_wait_ms,
        apps_dir=str(raw_options.get("apps_dir", "/data/apps")),
        web_enabled=bool(raw_options.get("web_enabled", True)),
        web_host=str(raw_options.get("web_host", "0.0.0.0")),
        web_port=int(raw_options.get("web_port", 8099)),
        web_base_path=_normalize_web_base_path(
            _env_or_default("UPS2MQTT_WEB_BASE_PATH", raw_options.get("web_base_path", "/"))
        ),
        devices=devices,
        raw=raw_options,
        ha_url=_clean_optional(
            _env_or_default("UPS2MQTT_HA_URL", raw_options.get("ha_url"))
        ),
        ha_token=_clean_optional(
            _env_or_default("UPS2MQTT_HA_TOKEN", raw_options.get("ha_token"))
        ),
        ha_bridge_enabled=ha_bridge_enabled,
    )
