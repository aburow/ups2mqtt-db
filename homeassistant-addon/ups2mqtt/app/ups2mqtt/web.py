# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from contextlib import nullcontext
import html
import json
import logging
import threading
from uuid import uuid4
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .catalog import CATALOG_DRIVER_KEYS, get_catalog_sensor_rows
from .capabilities import source_keys
from .database import Database as ProfileDatabase
from .diagnostics import check_config
from .icon_resolver import resolve_enabled_defaults
from .log_buffer import LogBuffer
from .model import DeviceConfig, ProfileConfig
from .store import DeviceStore
from .versions import APP_VERSION, BACKUP_SCHEMA_NAME, BACKUP_SCHEMA_VERSION

LOG = logging.getLogger("ups2mqtt.web")
AUDIT_LOG = logging.getLogger("ups2mqtt.audit")
FAVICON_PATH = Path(__file__).resolve().parent / "static" / "favicon.png"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
RUNTIME_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_TIMEZONE = "UTC"
DEFAULT_THEME = "system"
THEME_OPTIONS = ("light", "dark", "system")
APC_MODBUS_DRIVER_KEYS = {
    "apc_modbus_rack_pdu",
    "apc_modbus_smart",
    "apc_modbus_smt",
}
CSV_IMPORT_HEADERS = [
    "ID",
    "Source",
    "Host",
    "Port",
    "SNMPPort",
    "Unit",
    "SNMP",
    "Poll",
    "Name",
    "Location",
    "Debug",
    "KeepConnectionOpen",
    "Discovery",
    "Polling",
]
# Cache reference managed by catalog module.
APC_CATALOG_CACHE: dict[str, dict[str, list[dict[str, str]]]] = {}


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


_SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "credential",
)
_SENSITIVE_KEY_EXACT = {
    "snmp_community",
    "community",
}


def _redact_sensitive(data: Any) -> Any:
    if isinstance(data, dict):
        out: dict[Any, Any] = {}
        for key, value in data.items():
            key_text = str(key).strip().lower()
            if key_text in _SENSITIVE_KEY_EXACT or any(
                fragment in key_text for fragment in _SENSITIVE_KEY_FRAGMENTS
            ):
                out[key] = "***REDACTED***"
            else:
                out[key] = _redact_sensitive(value)
        return out
    if isinstance(data, list):
        return [_redact_sensitive(item) for item in data]
    return data


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True)


def _int_or_default(raw: str, default: int) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_from_form(data: dict[str, list[str]], key: str) -> bool:
    value = (data.get(key, [""])[0]).strip().lower()
    return value in {"1", "true", "on", "yes"}


def _decode_http_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _is_bitfield_sensor_key(sensor_key: str) -> bool:
    """Return True when a sensor key is a raw bitfield register marker."""
    return sensor_key.strip().lower().endswith("_bf")


def _validate_device_id(device_id: str) -> str:
    """Validate device ID format (non-empty, alphanumeric + underscore/dash)."""
    if not device_id:
        raise ValueError("Device ID is required")
    if not all(c.isalnum() or c in "-_" for c in device_id):
        raise ValueError(
            "Device ID can only contain alphanumeric characters, hyphens, and underscores"
        )
    return device_id


def _validate_host(host: str) -> str:
    """Validate host is non-empty (IP or hostname)."""
    if not host:
        raise ValueError("Host is required")
    if not all(c.isalnum() or c in ".-:" for c in host):
        raise ValueError("Host contains invalid characters")
    return host


def _validate_port(port: int) -> int:
    """Validate port is in valid range."""
    if not (1 <= port <= 65535):
        raise ValueError(f"Port must be between 1 and 65535, got {port}")
    return port


def _validate_unit_id(unit_id: int) -> int:
    """Validate unit ID is in valid range (Modbus typical range)."""
    if not (1 <= unit_id <= 247):
        raise ValueError(f"Unit ID must be between 1 and 247, got {unit_id}")
    return unit_id


def _validate_poll_interval(poll_interval: int | None) -> int | None:
    """Validate poll interval is positive if provided."""
    if poll_interval is not None and poll_interval <= 0:
        raise ValueError(f"Poll interval must be positive, got {poll_interval}")
    return poll_interval


def _normalize_timezone(value: str | None) -> str:
    candidate = str(value or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(candidate)
        return candidate
    except ZoneInfoNotFoundError:
        return DEFAULT_TIMEZONE


def _normalize_theme(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in THEME_OPTIONS:
        return candidate
    return DEFAULT_THEME


def _category_sort_key(name: str) -> tuple[int, str]:
    """Sort canonical sensor groups in UX order before alphabetical fallback."""
    normalized = (name or "").strip().lower()
    priority = {
        "core": 0,
        "diagnostic": 1,
        "extended": 2,
    }
    return (priority.get(normalized, 99), normalized)


def _timezone_choices() -> list[str]:
    names = sorted(available_timezones())
    return names if names else [DEFAULT_TIMEZONE]


def _format_utc_timestamp(raw: str, timezone_name: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return text
    zone_name = _normalize_timezone(timezone_name)
    try:
        local_value = parsed.astimezone(ZoneInfo(zone_name))
    except ZoneInfoNotFoundError:
        local_value = parsed
    return local_value.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_sensor_preferences(
    raw: dict[str, Any] | None,
    *,
    allowed_keys: set[str],
    allowed_poll_groups: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return normalized
    for key, values in raw.items():
        if (
            not isinstance(key, str)
            or key not in allowed_keys
            or not isinstance(values, dict)
        ):
            continue
        record: dict[str, Any] = {
            "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
        }
        poll_group = str(values.get("poll_group", "")).strip()
        if poll_group and (
            allowed_poll_groups is None or poll_group in allowed_poll_groups
        ):
            record["poll_group"] = poll_group
        normalized[key] = record
    return normalized


def _build_sensor_preferences_from_selected(
    *,
    selected_sensors: list[str] | None,
    available_keys: list[str],
    default_poll_groups: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    selected_set = {str(item) for item in (selected_sensors or []) if str(item)}
    poll_group_defaults = default_poll_groups or {}
    return {
        key: {
            "mqtt_enabled": key in selected_set,
            **(
                {"poll_group": str(poll_group_defaults[key]).strip()}
                if str(poll_group_defaults.get(key, "")).strip()
                else {}
            ),
        }
        for key in available_keys
    }


def _sensor_poll_group_defaults_from_profile(
    profile: dict[str, Any],
) -> dict[str, str]:
    defaults: dict[str, str] = {}

    def _record_key(raw_key: Any, raw_group: Any) -> None:
        key = str(raw_key).strip()
        if not key or _is_bitfield_sensor_key(key):
            return
        group = str(raw_group or "slow").strip() or "slow"
        defaults[key] = group

    for item in profile.get("registers", []):
        if isinstance(item, dict):
            _record_key(item.get("key"), item.get("poll_group", "slow"))
    for block in profile.get("register_blocks", []):
        if not isinstance(block, dict):
            continue
        group = str(block.get("poll_group", "slow")).strip() or "slow"
        metrics = block.get("metrics", [])
        if isinstance(metrics, list):
            for metric in metrics:
                _record_key(metric, group)
    oids = profile.get("oids", {})
    if isinstance(oids, dict):
        for key, spec in oids.items():
            if isinstance(spec, dict):
                _record_key(key, spec.get("poll_group", "slow"))

    for transport in ("modbus", "snmp"):
        transport_spec = profile.get(transport, {})
        if not isinstance(transport_spec, dict):
            continue
        for item in transport_spec.get("registers", []):
            if isinstance(item, dict):
                _record_key(item.get("key"), item.get("poll_group", "slow"))
        for block in transport_spec.get("register_blocks", []):
            if not isinstance(block, dict):
                continue
            group = str(block.get("poll_group", "slow")).strip() or "slow"
            metrics = block.get("metrics", [])
            if isinstance(metrics, list):
                for metric in metrics:
                    _record_key(metric, group)
        transport_oids = transport_spec.get("oids", {})
        if isinstance(transport_oids, dict):
            for key, spec in transport_oids.items():
                if isinstance(spec, dict):
                    _record_key(key, spec.get("poll_group", "slow"))
        for block in transport_spec.get("snmp_blocks", []):
            if not isinstance(block, dict):
                continue
            group = str(block.get("poll_group", "slow")).strip() or "slow"
            metrics = block.get("metrics", [])
            if isinstance(metrics, list):
                for metric in metrics:
                    _record_key(metric, group)
    return defaults


def _catalog_sensor_rows_for_driver(
    *, apps_dir: str, driver_key: str
) -> list[dict[str, str]]:
    """Fetch normalized sensor rows from the shared catalog for UI rendering."""
    return get_catalog_sensor_rows(driver_key=driver_key, apps_dir=apps_dir)


def _build_form_values(data: dict[str, list[str]]) -> DeviceConfig:
    device_id = _validate_device_id((data.get("id", [""])[0]).strip())
    source = (data.get("source", [""])[0]).strip()
    if not source:
        raise ValueError("Source is required")
    host = _validate_host((data.get("host", [""])[0]).strip())

    port = _validate_port(_int_or_default((data.get("port", [""])[0]), 502))
    snmp_port = _validate_port(_int_or_default((data.get("snmp_port", [""])[0]), 161))
    unit_id = _validate_unit_id(_int_or_default((data.get("unit_id", [""])[0]), 1))

    poll_interval_raw = (data.get("poll_interval", [""])[0]).strip()
    poll_interval: int | None = None
    if poll_interval_raw:
        try:
            poll_interval = _validate_poll_interval(int(poll_interval_raw))
        except ValueError as e:
            raise ValueError(f"Poll interval validation failed: {e}")

    return DeviceConfig(
        id=device_id,
        source=source,
        host=host,
        port=port,
        snmp_port=snmp_port,
        unit_id=unit_id,
        snmp_community=(data.get("snmp_community", ["public"])[0]).strip() or "public",
        poll_interval=poll_interval,
        name=(data.get("name", [""])[0]).strip() or None,
        location=(data.get("location", [""])[0]).strip() or None,
        debug_logging=_bool_from_form(data, "debug_logging"),
        keep_connection_open=_bool_from_form(data, "keep_connection_open"),
        device_uid=(data.get("device_uid", [""])[0]).strip(),
        discovery_enabled=_bool_from_form(data, "discovery_enabled"),
        polling_enabled=_bool_from_form(data, "polling_enabled"),
    )


def _clone_device(
    device: DeviceConfig,
    *,
    debug_logging: bool | None = None,
    keep_connection_open: bool | None = None,
    discovery_enabled: bool | None = None,
    polling_enabled: bool | None = None,
) -> DeviceConfig:
    return DeviceConfig(
        id=device.id,
        source=device.source,
        host=device.host,
        port=device.port,
        snmp_port=device.snmp_port,
        unit_id=device.unit_id,
        snmp_community=device.snmp_community,
        poll_interval=device.poll_interval,
        name=device.name,
        location=device.location,
        debug_logging=device.debug_logging if debug_logging is None else debug_logging,
        keep_connection_open=(
            device.keep_connection_open
            if keep_connection_open is None
            else keep_connection_open
        ),
        device_uid=device.device_uid,
        discovery_enabled=(
            device.discovery_enabled if discovery_enabled is None else discovery_enabled
        ),
        polling_enabled=(
            device.polling_enabled if polling_enabled is None else polling_enabled
        ),
        profile_uid=device.profile_uid,
        profile_mode=device.profile_mode,
        local_profile_payload=(
            dict(device.local_profile_payload)
            if isinstance(device.local_profile_payload, dict)
            else None
        ),
        local_selected_sensors=(
            [str(item) for item in device.local_selected_sensors]
            if device.local_selected_sensors is not None
            else None
        ),
        local_sensor_preferences=(
            {
                str(key): {
                    "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                    **(
                        {"poll_group": str(values.get("poll_group", "")).strip()}
                        if str(values.get("poll_group", "")).strip()
                        else {}
                    ),
                }
                for key, values in device.local_sensor_preferences.items()
                if isinstance(key, str) and isinstance(values, dict)
            }
            if isinstance(device.local_sensor_preferences, dict)
            else None
        ),
    )


def _generate_devices_csv(devices: list[DeviceConfig]) -> str:
    """Generate CSV string from a list of devices."""
    lines = [",".join(CSV_IMPORT_HEADERS)]
    for d in devices:
        lines.append(
            f"{d.id},{d.source},{d.host},{d.port},{d.snmp_port},{d.unit_id},{d.snmp_community},{d.poll_interval or ''},{(d.name or '').replace(',', ' ')},{(d.location or '').replace(',', ' ')},{d.debug_logging},{d.keep_connection_open},{d.discovery_enabled},{d.polling_enabled}"
        )
    return "\n".join(lines)


def _generate_devices_csv_template() -> str:
    return ",".join(CSV_IMPORT_HEADERS) + "\n"


def _is_default_profile_name(name: str) -> bool:
    return "[default]" in str(name).lower()


def _prepare_metrics_presentation(
    metrics: dict,
    devices: list[DeviceConfig],
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, object]:
    metrics_totals = metrics.get("totals", {})
    metrics_devices = metrics.get("devices", {})
    identity_by_uid = {
        (device.device_uid or device.id): {
            "device_id": device.id,
            "device_name": (device.name or ""),
            "display_name": (device.name or device.id),
        }
        for device in devices
    }

    rows: list[dict[str, str | int]] = []
    ordered_uids: list[str] = []
    for device in sorted(devices, key=lambda item: item.id.lower()):
        uid = device.device_uid or device.id
        if uid not in ordered_uids:
            ordered_uids.append(uid)
    for uid in sorted(metrics_devices.keys()):
        if uid not in ordered_uids:
            ordered_uids.append(uid)

    for device_uid in ordered_uids:
        item = metrics_devices.get(device_uid, {})
        identity = identity_by_uid.get(
            device_uid,
            {
                "device_id": device_uid,
                "device_name": "",
                "display_name": device_uid,
            },
        )
        rows.append(
            {
                "device_uid": device_uid,
                "device_id": str(identity["device_id"]),
                "device_name": str(identity["device_name"]),
                "name": str(identity["display_name"]),
                "name_with_uid": (
                    f"{identity['display_name']} ({device_uid})"
                    if str(identity["display_name"]) != device_uid
                    else device_uid
                ),
                "status": str(item.get("last_status", "unknown")),
                "started": int(item.get("polls_started", 0)),
                "success": int(item.get("polls_succeeded", 0)),
                "failed": int(item.get("polls_failed", 0)),
                "timeout": int(item.get("polls_timed_out", 0)),
                "min_ms": (
                    f"{float(item.get('min_duration_ms')):.1f}"
                    if item.get("min_duration_ms") is not None
                    else ""
                ),
                "avg_ms": f"{float(item.get('average_duration_ms', 0.0)):.1f}",
                "max_ms": (
                    f"{float(item.get('max_duration_ms')):.1f}"
                    if item.get("max_duration_ms") is not None
                    else ""
                ),
                "last_ms": (
                    f"{float(item.get('last_duration_ms')):.1f}"
                    if item.get("last_duration_ms") is not None
                    else ""
                ),
                "wait_ms": (
                    f"{float(item.get('last_wait_ms')):.1f}"
                    if item.get("last_wait_ms") is not None
                    else ""
                ),
                "poll_ms": (
                    f"{float(item.get('last_poll_ms')):.1f}"
                    if item.get("last_poll_ms") is not None
                    else ""
                ),
                "publish_ms": (
                    f"{float(item.get('last_publish_ms')):.1f}"
                    if item.get("last_publish_ms") is not None
                    else ""
                ),
                "cadence_min_ms": (
                    f"{float(item.get('cadence_min_ms')):.1f}"
                    if item.get("cadence_min_ms") is not None
                    else ""
                ),
                "cadence_avg_ms": (
                    f"{float(item.get('cadence_average_ms')):.1f}"
                    if item.get("cadence_average_ms") is not None
                    else ""
                ),
                "cadence_max_ms": (
                    f"{float(item.get('cadence_max_ms')):.1f}"
                    if item.get("cadence_max_ms") is not None
                    else ""
                ),
                "cadence_last_ms": (
                    f"{float(item.get('cadence_last_ms')):.1f}"
                    if item.get("cadence_last_ms") is not None
                    else ""
                ),
                "utilization": (
                    f"{(float(item.get('average_duration_ms', 0.0)) / float(item.get('cadence_average_ms'))):.2f}"
                    if int(item.get("polls_succeeded") or 0) > 0
                    and float(item.get("cadence_average_ms") or 0.0) > 0.0
                    else ""
                ),
                "values": int(item.get("last_values_count") or 0),
                "last_error": str(item.get("last_error") or ""),
                "updated_utc": _format_utc_timestamp(
                    str(item.get("last_update_utc") or ""),
                    timezone_name,
                ),
            }
        )

    totals = {
        "devices": len(rows),
        "polls_started": int(metrics_totals.get("polls_started", 0)),
        "polls_succeeded": int(metrics_totals.get("polls_succeeded", 0)),
        "polls_failed": int(metrics_totals.get("polls_failed", 0)),
        "polls_timed_out": int(metrics_totals.get("polls_timed_out", 0)),
    }

    backpressure = {
        "polls_in_flight": int(
            metrics.get("backpressure", {}).get("polls_in_flight", 0)
        ),
        "semaphore_available": int(
            metrics.get("backpressure", {}).get("semaphore_available", 0)
        ),
        "wait_pressure": dict(
            metrics.get("backpressure", {}).get("wait_pressure", {})
        ),
        "adaptive_concurrency": dict(
            metrics.get("backpressure", {}).get("adaptive_concurrency", {})
        ),
    }

    return {
        "generated_at_utc": _format_utc_timestamp(
            str(metrics.get("generated_at_utc", "")),
            timezone_name,
        ),
        "timezone_label": _normalize_timezone(timezone_name),
        "totals": totals,
        "total_failed_timeout": int(totals["polls_failed"])
        + int(totals["polls_timed_out"]),
        "backpressure": backpressure,
        "rows": rows,
    }


def _enrich_metrics_snapshot_with_identity(
    metrics_snapshot: dict,
    devices: list[DeviceConfig],
) -> dict:
    enriched = dict(metrics_snapshot)
    devices_section = {
        str(key): dict(value)
        for key, value in dict(metrics_snapshot.get("devices", {})).items()
    }
    enriched["devices"] = devices_section

    identity_map: dict[str, dict[str, str]] = {}
    for device in devices:
        device_uid = device.device_uid or device.id
        identity_map[device_uid] = {
            "device_uid": device_uid,
            "device_id": device.id,
            "device_name": device.name or "",
            "display_name": device.name or device.id,
        }

    for device_uid, payload in devices_section.items():
        identity = identity_map.get(
            device_uid,
            {
                "device_uid": device_uid,
                "device_id": device_uid,
                "device_name": "",
                "display_name": device_uid,
            },
        )
        payload["device_uid"] = identity["device_uid"]
        payload["device_id"] = identity["device_id"]
        payload["device_name"] = identity["device_name"]
        payload["display_name"] = identity["display_name"]

    enriched["device_identity"] = identity_map
    return enriched


def _prepare_logs_presentation(
    log_entries: list[object],
    timezone_name: str = DEFAULT_TIMEZONE,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for entry in log_entries:
        level = str(getattr(entry, "level", "INFO"))
        rows.append(
            {
                "ts": _format_utc_timestamp(
                    str(getattr(entry, "ts", "")),
                    timezone_name,
                ),
                "level": level,
                "level_class": f"log-{level}",
                "logger": str(getattr(entry, "logger", "")),
                "device": str(getattr(entry, "device", "") or ""),
                "message": str(getattr(entry, "message", "")),
            }
        )
    return rows


def _prepare_maintenance_presentation(
    *,
    capability_status: dict[str, Any],
    current_runtime_log_level: str,
) -> dict[str, object]:
    return {
        "source": str(capability_status.get("source", "unknown")),
        "profile_count": int(capability_status.get("profile_count", 0)),
        "apps_dir": str(capability_status.get("apps_dir", "/data/apps")),
        "max_concurrent_polls": str(capability_status.get("max_concurrent_polls", "")),
        "runtime_log_level": str(current_runtime_log_level),
        "runtime_log_levels": RUNTIME_LOG_LEVELS,
    }


def start_web_server(
    host: str,
    port: int,
    store: DeviceStore,
    get_source_names: Callable[[], list[str]],
    log_buffer: LogBuffer,
    get_capability_status: Callable[[], dict[str, Any]],
    trigger_capability_reload: Callable[[], None],
    trigger_republish_discovery: Callable[[], None],
    get_metrics_snapshot: Callable[[], dict],
    trigger_reload: Callable[[], None],
    trigger_metrics_drop: Callable[[str], None] | None = None,
    trigger_metrics_clear: Callable[[], None] | None = None,
    trigger_db_cleanup: Callable[[], dict[str, int]] | None = None,
    trigger_device_reinitialize: Callable[[str], None] | None = None,
    get_config: Callable[[], dict] | None = None,
    get_timezone: Callable[[], str] | None = None,
    set_timezone: Callable[[str], None] | None = None,
    get_theme: Callable[[], str] | None = None,
    set_theme: Callable[[str], None] | None = None,
    get_metadata_refresh_interval_seconds: Callable[[], int] | None = None,
    set_metadata_refresh_interval_seconds: Callable[[int], None] | None = None,
    get_idle_reconnect_seconds: Callable[[], float] | None = None,
    set_idle_reconnect_seconds: Callable[[float], None] | None = None,
    get_ha_bridge_enabled: Callable[[], bool] | None = None,
    set_ha_bridge_enabled: Callable[[bool], None] | None = None,
    get_capability_profiles: Callable[[], dict[str, dict[str, object]]] | None = None,
    get_cached_ha_payload_preview: (
        Callable[[DeviceConfig], dict[str, Any] | None] | None
    ) = None,
    web_base_path: str = "/",
) -> HTTPServer:
    def _normalize_base_path(value: str | None) -> str:
        text = str(value or "").strip()
        if not text:
            return "/"
        if not text.startswith("/"):
            text = f"/{text}"
        text = text.rstrip("/")
        return text or "/"

    normalized_base_path = _normalize_base_path(web_base_path)

    # nosemgrep: python.flask.security.xss.audit.direct-use-of-jinja2.direct-use-of-jinja2
    templates = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    timezone_options = _timezone_choices()
    timezone_getter = get_timezone or (lambda: DEFAULT_TIMEZONE)
    timezone_setter = set_timezone or (lambda value: None)
    theme_getter = get_theme or (lambda: DEFAULT_THEME)
    theme_setter = set_theme or (lambda value: None)
    metadata_refresh_getter = get_metadata_refresh_interval_seconds or (lambda: 3600)
    metadata_refresh_setter = (
        set_metadata_refresh_interval_seconds or (lambda value: None)
    )
    idle_reconnect_getter = get_idle_reconnect_seconds or (lambda: 300.0)
    idle_reconnect_setter = set_idle_reconnect_seconds or (lambda value: None)
    ha_bridge_enabled_getter = get_ha_bridge_enabled or (lambda: False)
    ha_bridge_enabled_setter = set_ha_bridge_enabled or (lambda value: None)
    capability_profiles_getter = get_capability_profiles or (lambda: {})
    profile_device_info_keys = {
        # Canonical device-info/identity-like fields used by existing contract adapters.
        "manufacturer",
        "model",
        "serial_number",
        "sw_version",
        "hw_version",
        "configuration_url",
        "firmware",
        "firmware_version",
        "firmware_date",
    }

    device_filter_keys = (
        "device_filter_id",
        "device_filter_name",
        "device_filter_location",
        "device_filter_host",
        "device_filter_profile",
    )

    def _device_filter_values_from_params(
        params: dict[str, list[str]] | None,
    ) -> dict[str, str]:
        params = params or {}
        return {key: (params.get(key, [""])[0]).strip() for key in device_filter_keys}

    def _device_filter_values_from_data(
        data: dict[str, list[str]] | None,
    ) -> dict[str, str]:
        data = data or {}
        return {key: (data.get(key, [""])[0]).strip() for key in device_filter_keys}

    def _device_matches_filters(
        device: DeviceConfig,
        filters: dict[str, str],
        profile_name_by_uid: dict[str, str],
    ) -> bool:
        if filters["device_filter_id"] and filters["device_filter_id"].lower() not in (
            device.id.lower()
        ):
            return False
        if filters["device_filter_name"] and filters["device_filter_name"].lower() not in (
            (device.name or "").lower()
        ):
            return False
        location_text = (device.location or "").strip() or "-"
        if filters["device_filter_location"] and filters[
            "device_filter_location"
        ].lower() not in (location_text.lower()):
            return False
        if filters["device_filter_host"] and filters[
            "device_filter_host"
        ].lower() not in (device.host.lower()):
            return False
        profile_text = ""
        profile_uid = str(device.profile_uid or "").strip()
        if profile_uid and profile_name_by_uid.get(profile_uid):
            profile_text = str(profile_name_by_uid.get(profile_uid, ""))
        elif profile_uid:
            profile_text = profile_uid
        else:
            profile_text = "Legacy Local"
        if filters["device_filter_profile"] and filters[
            "device_filter_profile"
        ].lower() not in (profile_text.lower()):
            return False
        return True

    def _filtered_sorted_devices(filters: dict[str, str]) -> list[DeviceConfig]:
        profile_name_by_uid = {
            item.profile_uid: item.name for item in _load_profiles() if item.profile_uid
        }
        return sorted(
            (
                item
                for item in store.list_devices()
                if _device_matches_filters(item, filters, profile_name_by_uid)
            ),
            key=lambda item: item.id.lower(),
        )

    def _profile_db():
        return getattr(store, "_db", None)

    def _load_profiles() -> list[ProfileConfig]:
        db = _profile_db()
        if db is None or not hasattr(db, "load_profiles"):
            return []
        return db.load_profiles()

    def _get_profile(profile_uid: str) -> ProfileConfig | None:
        for item in _load_profiles():
            if item.profile_uid == profile_uid:
                return item
        return None

    def _is_profile_driver_eligible(
        driver_key: str, profile: dict[str, object] | None
    ) -> bool:
        if not driver_key:
            return False
        if driver_key.lower().startswith("nut"):
            return False
        protocol = str((profile or {}).get("protocol", "")).strip().lower()
        if protocol == "nut":
            return False
        return bool(profile)

    def _eligible_profile_drivers() -> dict[str, dict[str, object]]:
        raw = capability_profiles_getter() or {}
        out: dict[str, dict[str, object]] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            if not _is_profile_driver_eligible(key, value):
                continue
            out[key] = value
        return dict(sorted(out.items(), key=lambda item: item[0]))

    def _profile_default_payload(
        driver_key: str,
        contract_profile: dict[str, object],
    ) -> dict[str, object]:
        poll_groups = contract_profile.get("poll_groups", {})
        poll_group_values: dict[str, int] = {}
        if isinstance(poll_groups, dict):
            for group_name, spec in poll_groups.items():
                if not isinstance(group_name, str) or not isinstance(spec, dict):
                    continue
                poll_group_values[group_name] = _int_or_default(
                    str(spec.get("interval_s", 60)),
                    60,
                )

        key_precedence_values: dict[str, str] = {}
        key_precedence = contract_profile.get("key_precedence", {})
        if isinstance(key_precedence, dict):
            for key_name, source in key_precedence.items():
                if not isinstance(key_name, str):
                    continue
                source_text = str(source).strip().lower()
                if source_text in {"modbus", "snmp"}:
                    key_precedence_values[key_name] = source_text

        return {
            "driver_key": driver_key,
            "poll_groups": poll_group_values,
            "key_precedence": key_precedence_values,
        }

    def _profile_default_enabled_map(
        driver_key: str,
        contract_profile: dict[str, object],
    ) -> dict[str, bool]:
        keys = [str(item) for item in source_keys(contract_profile) if str(item)]
        apps_dir = str(get_capability_status().get("apps_dir", "/data/apps"))
        try:
            return resolve_enabled_defaults(
                driver_key,
                keys,
                apps_dir=apps_dir,
            )
        except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            return {key: True for key in keys}

    def _profile_selected_sensor_defaults(
        driver_key: str,
        contract_profile: dict[str, object],
    ) -> list[str]:
        enabled_defaults = _profile_default_enabled_map(driver_key, contract_profile)
        selected = [
            key
            for key in sorted(enabled_defaults)
            if bool(enabled_defaults.get(key, True))
        ]
        return sorted(selected)

    def _profile_allowed_sensor_keys(
        driver_key: str,
        contract_profile: dict[str, object],
    ) -> list[str]:
        keys = {
            key
            for item in source_keys(contract_profile)
            for key in [str(item).strip()]
            if key and not _is_bitfield_sensor_key(key)
        }
        apps_dir = str(get_capability_status().get("apps_dir", "/data/apps"))
        for item in _catalog_sensor_rows_for_driver(
            apps_dir=apps_dir,
            driver_key=driver_key,
        ):
            key = str(item.get("key", "")).strip()
            if key and not _is_bitfield_sensor_key(key):
                keys.add(key)
        return sorted(keys)

    def _profile_editor_context(
        *,
        profile_uid: str,
        profile_name: str,
        driver_key: str,
        config_payload: dict[str, object] | None = None,
        selected_sensors: list[str] | None = None,
        sensor_preferences: dict[str, dict[str, Any]] | None = None,
        comments: str = "",
        error_message: str = "",
        is_protected_profile: bool = False,
        save_action_path: str = "/htmx/profiles/actions/upsert",
        copy_source_name: str = "",
    ) -> dict[str, object]:
        eligible = _eligible_profile_drivers()
        driver_options = list(eligible.keys())
        selected_driver = (
            driver_key.strip()
            if driver_key.strip()
            else (driver_options[0] if driver_options else "")
        )
        contract_profile = eligible.get(selected_driver)
        driver_contract_missing = bool(
            profile_uid.strip() and selected_driver and contract_profile is None
        )
        if contract_profile is None:
            return {
                "profile_uid": profile_uid,
                "profile_name": profile_name,
                "driver_key": selected_driver,
                "driver_options": driver_options,
                "poll_group_rows": [],
                "key_precedence_rows": [],
                "sensor_rows": [],
                "sensor_rows_defaults": [],
                "sensor_rows_device_info": [],
                "sensor_rows_other": [],
                "sensor_rows_are_dual_toggle": False,
                "driver_contract_missing": driver_contract_missing,
                "can_save_profile": False,
                "profile_is_protected": is_protected_profile,
                "profile_comments": comments,
                "save_action_path": save_action_path,
                "copy_source_name": copy_source_name,
                "error_message": error_message
                or (
                    f"Driver {selected_driver} is not profile-eligible"
                    if selected_driver
                    else "No profile-eligible drivers are available"
                ),
                "sensor_count": 0,
                "sensor_poll_group_choices": [],
            }

        defaults = _profile_default_payload(selected_driver, contract_profile)
        poll_group_choices = sorted(
            str(name)
            for name in dict(defaults.get("poll_groups", {}))
            if str(name).strip()
        )
        allowed_poll_groups = set(poll_group_choices)
        sensor_poll_group_defaults = _sensor_poll_group_defaults_from_profile(
            contract_profile
        )
        merged_payload = dict(defaults)
        if isinstance(config_payload, dict):
            incoming_groups = config_payload.get("poll_groups", {})
            incoming_precedence = config_payload.get("key_precedence", {})
            if isinstance(incoming_groups, dict):
                merged_payload["poll_groups"] = dict(defaults["poll_groups"]) | {
                    str(key): _int_or_default(str(value), 60)
                    for key, value in incoming_groups.items()
                    if str(key) in dict(defaults["poll_groups"])
                }
            if isinstance(incoming_precedence, dict):
                merged_payload["key_precedence"] = dict(defaults["key_precedence"]) | {
                    str(key): str(value)
                    for key, value in incoming_precedence.items()
                    if str(key) in dict(defaults["key_precedence"])
                    and str(value) in {"modbus", "snmp"}
                }

        if selected_sensors is None:
            merged_selected = _profile_selected_sensor_defaults(
                selected_driver, contract_profile
            )
        else:
            merged_selected = sorted(
                {str(item) for item in selected_sensors if str(item)}
            )

        try:
            available_sensor_keys = _profile_allowed_sensor_keys(
                selected_driver,
                contract_profile,
            )
        except ValueError as err:
            return {
                "profile_uid": profile_uid,
                "profile_name": profile_name,
                "driver_key": selected_driver,
                "driver_options": driver_options,
                "poll_group_rows": sorted(
                    dict(merged_payload.get("poll_groups", {})).items(),
                    key=lambda item: item[0],
                ),
                "key_precedence_rows": sorted(
                    dict(merged_payload.get("key_precedence", {})).items(),
                    key=lambda item: item[0],
                ),
                "sensor_rows": [],
                "sensor_rows_defaults": [],
                "sensor_rows_device_info": [],
                "sensor_rows_other": [],
                "sensor_rows_are_dual_toggle": selected_driver in CATALOG_DRIVER_KEYS,
                "driver_contract_missing": False,
                "can_save_profile": False,
                "profile_is_protected": is_protected_profile,
                "profile_comments": comments,
                "save_action_path": save_action_path,
                "copy_source_name": copy_source_name,
                "error_message": str(err),
                "sensor_count": 0,
                "sensor_poll_group_choices": poll_group_choices,
            }
        default_enabled = _profile_default_enabled_map(
            selected_driver,
            contract_profile,
        )
        selected_set = set(merged_selected)
        merged_preferences = _normalize_sensor_preferences(
            sensor_preferences,
            allowed_keys=set(available_sensor_keys),
            allowed_poll_groups=allowed_poll_groups,
        )
        if not merged_preferences:
            merged_preferences = _build_sensor_preferences_from_selected(
                selected_sensors=merged_selected,
                available_keys=available_sensor_keys,
                default_poll_groups=sensor_poll_group_defaults,
            )
        for key in available_sensor_keys:
            if key not in merged_preferences:
                merged_preferences[key] = {
                    "mqtt_enabled": key in selected_set,
                    "poll_group": str(sensor_poll_group_defaults.get(key, "slow")),
                }
        # Get catalog rows
        apps_dir = str(get_capability_status().get("apps_dir", "/data/apps"))
        catalog_rows = _catalog_sensor_rows_for_driver(
            apps_dir=apps_dir,
            driver_key=selected_driver,
        )

        # Build catalog metadata map
        catalog_by_key: dict[str, dict[str, str]] = {}
        for item in catalog_rows:
            key = str(item.get("key", "")).strip()
            if key:
                catalog_by_key[key] = item

        # Build sensor rows using the same grouping structure as device edit:
        # catalog-backed keys are grouped by category (core/diagnostic/extended),
        # with non-catalog fallback keys retained in the unified bucket.
        available_sensor_set = set(available_sensor_keys)
        unified_keys = sorted(available_sensor_set - set(catalog_by_key.keys()))

        # Build unified fallback sensor rows
        unified_rows: list[dict[str, object]] = []
        for key in unified_keys:
            prefs = merged_preferences.get(
                key,
                {
                    "mqtt_enabled": key in selected_set,
                },
            )
            unified_rows.append(
                {
                    "key": key,
                    "selected": bool(prefs.get("mqtt_enabled", False)),
                    "mqtt_enabled": bool(prefs.get("mqtt_enabled", False)),
                    "default_enabled": bool(default_enabled.get(key, True)),
                    "label": key,
                    "category": "other",
                    "unit": "",
                    "source": "",
                    "aliases": "",
                    "reference": "",
                    "from_catalog": False,
                    "poll_group": str(
                        prefs.get(
                            "poll_group",
                            sensor_poll_group_defaults.get(key, "slow"),
                        )
                    ),
                }
            )

        # Build catalog-backed rows, grouped by category
        catalog_groups: dict[str, list[dict[str, object]]] = {}
        for key in sorted(available_sensor_set & set(catalog_by_key.keys())):
            sensor_meta = catalog_by_key[key]
            category = str(sensor_meta.get("category", "")).strip() or "Other"
            prefs = merged_preferences.get(
                key,
                {
                    "mqtt_enabled": key in selected_set,
                },
            )
            row = {
                "key": key,
                "selected": bool(prefs.get("mqtt_enabled", False)),
                "mqtt_enabled": bool(prefs.get("mqtt_enabled", False)),
                "default_enabled": False,  # Catalog-only sensors are not contract defaults
                "label": str(sensor_meta.get("label", key)),
                "category": category,
                "unit": str(sensor_meta.get("unit", "")),
                "source": str(sensor_meta.get("source", "")),
                "aliases": str(sensor_meta.get("aliases", "")),
                "reference": str(sensor_meta.get("reference", "")),
                "from_catalog": True,
                "poll_group": str(
                    prefs.get(
                        "poll_group",
                        sensor_poll_group_defaults.get(key, "slow"),
                    )
                ),
            }
            if category not in catalog_groups:
                catalog_groups[category] = []
            catalog_groups[category].append(row)

        # Sort catalog groups in UX order: core, diagnostic, extended, then others
        sorted_catalog_groups = sorted(
            catalog_groups.items(),
            key=lambda item: _category_sort_key(item[0]),
        )

        # Filter merged_selected to only include available keys
        available_set = set(available_sensor_keys)
        merged_selected = [key for key in merged_selected if key in available_set]

        # Legacy grouping retained for backwards compatibility only
        defaults_rows: list[dict[str, object]] = []
        device_info_rows: list[dict[str, object]] = []
        other_rows: list[dict[str, object]] = []
        for row in unified_rows:
            key = str(row["key"])
            if key in profile_device_info_keys:
                device_info_rows.append(row)
            elif bool(row.get("default_enabled", True)):
                defaults_rows.append(row)
            else:
                other_rows.append(row)

        return {
            "profile_uid": profile_uid,
            "profile_name": profile_name,
            "driver_key": selected_driver,
            "driver_options": driver_options,
            "poll_group_rows": sorted(
                dict(merged_payload.get("poll_groups", {})).items(),
                key=lambda item: item[0],
            ),
            "key_precedence_rows": sorted(
                dict(merged_payload.get("key_precedence", {})).items(),
                key=lambda item: item[0],
            ),
            "sensor_rows": defaults_rows + device_info_rows + other_rows,
            "sensor_rows_defaults": defaults_rows,
            "sensor_rows_device_info": device_info_rows,
            "sensor_rows_other": other_rows,
            "sensor_rows_are_dual_toggle": selected_driver in CATALOG_DRIVER_KEYS,
            "unified_sensor_rows": unified_rows,
            "catalog_sensor_groups": sorted_catalog_groups,
            "has_catalog_sensors": bool(sorted_catalog_groups),
            "profile_comments": comments,
            "save_action_path": save_action_path,
            "copy_source_name": copy_source_name,
            "error_message": error_message,
            "sensor_count": len(available_sensor_keys),
            "driver_contract_missing": False,
            "can_save_profile": not is_protected_profile,
            "profile_is_protected": is_protected_profile,
            "sensor_poll_group_choices": poll_group_choices,
        }

    def _render_htmx_profiles_form(
        *,
        profile_uid: str = "",
        profile_name: str = "",
        driver_key: str = "",
        config_payload: dict[str, object] | None = None,
        selected_sensors: list[str] | None = None,
        sensor_preferences: dict[str, dict[str, Any]] | None = None,
        comments: str = "",
        error_message: str = "",
        is_protected_profile: bool = False,
        save_action_path: str = "/htmx/profiles/actions/upsert",
        copy_source_name: str = "",
    ) -> str:
        context = _profile_editor_context(
            profile_uid=profile_uid,
            profile_name=profile_name,
            driver_key=driver_key,
            config_payload=config_payload,
            selected_sensors=selected_sensors,
            sensor_preferences=sensor_preferences,
            comments=comments,
            error_message=error_message,
            is_protected_profile=is_protected_profile,
            save_action_path=save_action_path,
            copy_source_name=copy_source_name,
        )
        return templates.get_template("htmx/profiles_form.html").render(**context)

    def _render_htmx_profiles_form_for_new(
        *,
        profile_uid: str = "",
        profile_name: str = "",
        driver_key: str = "",
    ) -> str:
        return _render_htmx_profiles_form(
            profile_uid=profile_uid,
            profile_name=profile_name,
            driver_key=driver_key,
            config_payload=None,
            selected_sensors=None,
            sensor_preferences=None,
            comments="",
            error_message="",
        )

    def _devices_bound_to_profile(profile_uid: str) -> list[DeviceConfig]:
        if not profile_uid:
            return []
        return [
            item
            for item in store.list_devices()
            if str(item.profile_uid) == profile_uid
        ]

    def _render_htmx_profiles_panel(
        *,
        form_html: str | None = None,
    ) -> str:
        profiles = sorted(_load_profiles(), key=lambda item: item.name.lower())
        devices_by_profile_uid: dict[str, list[DeviceConfig]] = {}
        for device in store.list_devices():
            profile_uid = str(device.profile_uid or "").strip()
            if not profile_uid:
                continue
            devices_by_profile_uid.setdefault(profile_uid, []).append(device)
        profile_rows = [
            {
                "profile_uid": item.profile_uid,
                "name": item.name,
                "driver_key": item.driver_key,
                "sensor_count": len(item.selected_sensors),
                "usage_count": int(
                    len(devices_by_profile_uid.get(item.profile_uid, []))
                ),
                "affected_device_ids": [
                    device.id
                    for device in devices_by_profile_uid.get(item.profile_uid, [])
                ],
                "is_protected": bool(item.is_protected),
            }
            for item in profiles
        ]
        return templates.get_template("htmx/panels/profiles_panel.html").render(
            profiles=profile_rows,
            form_html=form_html
            if form_html is not None
            else _render_htmx_profiles_form(),
        )

    def _render_htmx_devices_table(
        filters: dict[str, str] | None = None,
    ) -> str:
        effective_filters = filters or _device_filter_values_from_params(None)
        profile_name_by_uid = {
            item.profile_uid: item.name for item in _load_profiles() if item.profile_uid
        }
        return templates.get_template("htmx/devices_table.html").render(
            devices=_filtered_sorted_devices(effective_filters),
            filters=effective_filters,
            profile_name_by_uid=profile_name_by_uid,
        )

    def _render_htmx_devices_panel(
        filters: dict[str, str] | None = None,
    ) -> str:
        effective_filters = filters or _device_filter_values_from_params(None)
        return templates.get_template("htmx/panels/devices_panel.html").render(
            devices_table_html=_render_htmx_devices_table(effective_filters),
            filters=effective_filters,
        )

    def _render_htmx_device_ha_payload_modal(
        *,
        device: DeviceConfig | None = None,
        not_found_device_id: str = "",
    ) -> tuple[str, HTTPStatus]:
        if device is None:
            return (
                templates.get_template("htmx/device_ha_payload_modal.html").render(
                    not_found=True,
                    device_id=not_found_device_id,
                ),
                HTTPStatus.OK,
            )
        preview = (
            get_cached_ha_payload_preview(device)
            if get_cached_ha_payload_preview is not None
            else None
        )
        if not isinstance(preview, dict):
            preview = {}
        redacted = _redact_sensitive(preview)
        cached_state = redacted.get("cached_state")
        cached_metadata = redacted.get("cached_metadata")
        entities = redacted.get("entities")
        has_cached_data = bool(cached_state) or bool(cached_metadata) or bool(entities)
        metadata_map = (
            cached_metadata if isinstance(cached_metadata, dict) else {}
        )
        topics_map = (
            redacted.get("topics") if isinstance(redacted.get("topics"), dict) else {}
        )
        return (
            templates.get_template("htmx/device_ha_payload_modal.html").render(
                not_found=False,
                has_cached_data=has_cached_data,
                device=device,
                identity=str(redacted.get("identity", device.device_uid or device.id)),
                manufacturer=metadata_map.get("manufacturer", ""),
                model=metadata_map.get("model", ""),
                topics=topics_map,
                entities=entities if isinstance(entities, list) else [],
                metadata_json=_pretty_json(metadata_map),
                state_json=_pretty_json(
                    cached_state if isinstance(cached_state, dict) else {}
                ),
                raw_json=_pretty_json(redacted),
            ),
            HTTPStatus.OK,
        )

    def _render_htmx_metrics_panel() -> str:
        metrics = get_metrics_snapshot()
        prepared_metrics = _prepare_metrics_presentation(
            metrics=metrics,
            devices=store.list_devices(),
            timezone_name=timezone_getter(),
        )
        capability_status = get_capability_status()

        return templates.get_template("htmx/panels/metrics_panel.html").render(
            generated_at_utc=str(prepared_metrics["generated_at_utc"]),
            timezone_label=str(prepared_metrics["timezone_label"]),
            totals=prepared_metrics["totals"],
            total_failed_timeout=int(prepared_metrics["total_failed_timeout"]),
            backpressure=prepared_metrics["backpressure"],
            max_concurrent_polls=capability_status.get("max_concurrent_polls", "?"),
            rows=prepared_metrics["rows"],
        )

    def _render_htmx_logs_panel(params: dict[str, list[str]] | None = None) -> str:
        params = params or {}
        log_level = params.get("log_level", [""])[0]
        log_logger = params.get("log_logger", [""])[0]
        log_contains = params.get("log_contains", [""])[0]
        log_device = params.get("log_device", [""])[0]
        log_limit = _int_or_default(params.get("log_limit", ["150"])[0], 150)

        filtered_logs = log_buffer.query(
            level=log_level,
            logger=log_logger,
            contains=log_contains,
            device=log_device,
            limit=log_limit,
        )
        timezone_name = _normalize_timezone(timezone_getter())
        prepared_logs = _prepare_logs_presentation(
            filtered_logs,
            timezone_name=timezone_name,
        )
        return templates.get_template("htmx/panels/logs_panel.html").render(
            filters={
                "log_level": log_level,
                "log_logger": log_logger,
                "log_contains": log_contains,
                "log_device": log_device,
                "log_limit": log_limit,
            },
            log_count=log_buffer.count(),
            log_capacity=log_buffer.capacity(),
            timezone_label=timezone_name,
            rows=prepared_logs,
        )

    def _render_htmx_configuration_panel() -> str:
        timezone_name = _normalize_timezone(timezone_getter())
        theme_name = _normalize_theme(theme_getter())
        return templates.get_template("htmx/panels/configuration_panel.html").render(
            timezone_value=timezone_name,
            timezone_options=timezone_options,
            theme_value=theme_name,
            theme_options=THEME_OPTIONS,
            metadata_refresh_interval_seconds=max(1, int(metadata_refresh_getter())),
            idle_reconnect_seconds=max(1.0, float(idle_reconnect_getter())),
            ha_bridge_enabled=bool(ha_bridge_enabled_getter()),
            runtime_log_level=logging.getLevelName(
                logging.getLogger().getEffectiveLevel()
            ),
            runtime_log_levels=RUNTIME_LOG_LEVELS,
        )

    def _render_htmx_maintenance_panel() -> str:
        maintenance = _prepare_maintenance_presentation(
            capability_status=get_capability_status(),
            current_runtime_log_level=logging.getLevelName(
                logging.getLogger().getEffectiveLevel()
            ),
        )
        return templates.get_template("htmx/panels/maintenance_panel.html").render(
            maintenance=maintenance
        )

    def _sidebar_version_items() -> list[dict[str, str]]:
        return [
            {"label": "App", "value": APP_VERSION},
            {
                "label": "Backup schema",
                "value": f"{BACKUP_SCHEMA_NAME} v{BACKUP_SCHEMA_VERSION}",
            },
        ]

    def _execute_maintenance_action(
        action: str,
        data: dict[str, list[str]],
    ) -> tuple[bool, str, str]:
        if action == "init_capabilities":
            trigger_capability_reload()
            return True, "Capability import reload triggered", ""
        if action == "republish_discovery":
            trigger_republish_discovery()
            return True, "MQTT discovery republish triggered", ""
        if action == "cleanup_db":
            if not trigger_db_cleanup:
                AUDIT_LOG.warning(
                    "maintenance action=cleanup_db status=unavailable reason=%s",
                    "db_cleanup_not_available",
                )
                return False, "", "DB cleanup not available"
            result = trigger_db_cleanup()
            trigger_reload()
            AUDIT_LOG.info(
                "maintenance action=cleanup_db status=success devices_removed=%d metrics_removed_memory=%d",
                int(result.get("devices_removed", 0)),
                int(result.get("metrics_removed_memory", 0)),
            )
            return (
                True,
                (
                    "SQLite cleanup complete: "
                    f"devices_removed={int(result.get('devices_removed', 0))} "
                    f"metrics_removed_memory={int(result.get('metrics_removed_memory', 0))}"
                ),
                "",
            )
        if action == "remove_all_devices":
            removed = 0
            for device in list(store.list_devices()):
                if store.delete_by_uid(device.device_uid):
                    removed += 1
                    if trigger_metrics_drop:
                        identity = device.device_uid or device.id
                        trigger_metrics_drop(identity)
                        if identity != device.id:
                            trigger_metrics_drop(device.id)
            trigger_reload()
            AUDIT_LOG.info(
                "maintenance action=remove_all_devices status=success removed=%d",
                removed,
            )
            return True, f"Removed {removed} device(s)", ""
        if action == "set_log_level":
            level_name = data.get("runtime_log_level", [""])[0].strip().upper()
            if level_name not in set(RUNTIME_LOG_LEVELS):
                return False, "", f"Invalid log level: {level_name}"
            logging.getLogger().setLevel(level_name)
            return True, f"Runtime log level set to {level_name}", ""
        return False, "", f"Unsupported maintenance action: {action or '(empty)'}"

    def _import_devices_from_csv_data(csv_data: str) -> int:
        import csv as csv_module
        import io

        reader = csv_module.reader(io.StringIO(csv_data))
        lines = list(reader)
        if not lines:
            return 0
        header = [str(item).strip() for item in lines[0]]
        header_map = {item.lower(): index for index, item in enumerate(header)}
        has_location_column = "location" in header_map
        count = 0
        db = _profile_db()
        tx = db.transaction() if db is not None else nullcontext()
        with tx:
            for row in lines[1:]:
                required_cells = 13 if has_location_column else 12
                if len(row) < required_cells:
                    LOG.warning("Skipping malformed CSV line: %s", row)
                    continue
                try:
                    def _cell(index: int, default: str = "") -> str:
                        if 0 <= index < len(row):
                            return str(row[index]).strip()
                        return default

                    def _cell_by_header(name: str, fallback_index: int) -> str:
                        index = header_map.get(name.lower())
                        if index is not None:
                            return _cell(index)
                        return _cell(fallback_index)

                    dev = DeviceConfig(
                        id=_cell_by_header("ID", 0),
                        source=_cell_by_header("Source", 1),
                        host=_cell_by_header("Host", 2),
                        port=int(_cell_by_header("Port", 3) or 502),
                        snmp_port=int(_cell_by_header("SNMPPort", -1) or 161),
                        unit_id=int(_cell_by_header("Unit", 4) or 1),
                        snmp_community=(_cell_by_header("SNMP", 5) or "public"),
                        poll_interval=(
                            int(_cell_by_header("Poll", 6) or 0)
                            if _cell_by_header("Poll", 6)
                            else None
                        ),
                        name=(_cell_by_header("Name", 7) or None),
                        location=(
                            _cell_by_header("Location", 8 if has_location_column else -1)
                            or None
                        ),
                        debug_logging=str(
                            _cell_by_header("Debug", 9 if has_location_column else 8)
                        ).lower()
                        in {"true", "1", "yes"},
                        keep_connection_open=str(
                            _cell_by_header(
                                "KeepConnectionOpen", 10 if has_location_column else 9
                            )
                        ).lower()
                        in {"true", "1", "yes"},
                        discovery_enabled=str(
                            _cell_by_header("Discovery", 11 if has_location_column else 10)
                        ).lower()
                        in {"true", "1", "yes"},
                        polling_enabled=str(
                            _cell_by_header("Polling", 12 if has_location_column else 11)
                        ).lower()
                        in {"true", "1", "yes"},
                    )
                    store.upsert(dev)
                    count += 1
                except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                    LOG.exception("CSV import error on row %r: %s", row, err)
        trigger_reload()
        return count

    def _profile_snapshot_dict(profile: ProfileConfig) -> dict[str, Any]:
        return {
            "profile_uid": str(profile.profile_uid),
            "name": str(profile.name),
            "driver_key": str(profile.driver_key),
            "config_payload": (
                dict(profile.config_payload)
                if isinstance(profile.config_payload, dict)
                else {}
            ),
            "selected_sensors": [str(item) for item in profile.selected_sensors],
            "sensor_preferences": (
                {
                    str(key): {
                        "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                        **(
                            {"poll_group": str(values.get("poll_group", "")).strip()}
                            if str(values.get("poll_group", "")).strip()
                            else {}
                        ),
                    }
                    for key, values in profile.sensor_preferences.items()
                    if isinstance(key, str) and isinstance(values, dict)
                }
                if isinstance(profile.sensor_preferences, dict)
                else {}
            ),
            "comments": str(profile.comments or ""),
            "is_protected": bool(profile.is_protected),
        }

    def _device_export_payload(devices: list[DeviceConfig]) -> dict[str, Any]:
        profiles_by_uid = {
            item.profile_uid: item for item in _load_profiles() if item.profile_uid
        }
        included_profile_uids: set[str] = set()
        profile_snapshots: list[dict[str, Any]] = []
        exported_devices: list[dict[str, Any]] = []

        for device in devices:
            bound_profile = (
                profiles_by_uid.get(device.profile_uid) if device.profile_uid else None
            )
            driver_key = (
                bound_profile.driver_key
                if bound_profile is not None
                else str(device.source or "")
            )
            raw_mode = str(device.profile_mode or "").strip().lower()
            export_mode = raw_mode if raw_mode in {"global", "local", "default"} else ""
            if export_mode == "":
                if raw_mode == "local":
                    export_mode = "local"
                elif bound_profile is not None and _is_default_profile_name(
                    bound_profile.name
                ):
                    export_mode = "default"
                elif bound_profile is not None:
                    export_mode = "global"
                else:
                    export_mode = "default"

            profile_uid_value: str | None = None
            profile_name_value: str | None = None
            if export_mode == "global":
                if bound_profile is not None:
                    profile_uid_value = bound_profile.profile_uid
                    profile_name_value = bound_profile.name
                    if bound_profile.profile_uid not in included_profile_uids:
                        included_profile_uids.add(bound_profile.profile_uid)
                        profile_snapshots.append(_profile_snapshot_dict(bound_profile))
            elif export_mode == "local":
                if bound_profile is not None:
                    profile_uid_value = bound_profile.profile_uid
                    profile_name_value = bound_profile.name
                    if bound_profile.profile_uid not in included_profile_uids:
                        included_profile_uids.add(bound_profile.profile_uid)
                        profile_snapshots.append(_profile_snapshot_dict(bound_profile))
            else:
                profile_uid_value = None
                profile_name_value = (
                    bound_profile.name if bound_profile is not None else None
                )

            exported_devices.append(
                {
                    "device_uid": str(device.device_uid),
                    "name": str(device.name or ""),
                    "location": str(device.location or ""),
                    "driver_key": str(driver_key),
                    "profile_mode": export_mode,
                    "profile_uid": profile_uid_value,
                    "profile_name": profile_name_value,
                    "config": {
                        "id": str(device.id),
                        "host": str(device.host),
                        "port": int(device.port),
                        "snmp_port": int(device.snmp_port),
                        "unit_id": int(device.unit_id),
                        "snmp_community": str(device.snmp_community),
                        "poll_interval": (
                            int(device.poll_interval)
                            if device.poll_interval is not None
                            else None
                        ),
                        "debug_logging": bool(device.debug_logging),
                        "keep_connection_open": bool(device.keep_connection_open),
                        "discovery_enabled": bool(device.discovery_enabled),
                        "polling_enabled": bool(device.polling_enabled),
                    },
                    "local_profile_payload": (
                        dict(device.local_profile_payload)
                        if isinstance(device.local_profile_payload, dict)
                        else None
                    ),
                    "local_selected_sensors": (
                        [str(item) for item in device.local_selected_sensors]
                        if device.local_selected_sensors is not None
                        else None
                    ),
                    "local_sensor_preferences": (
                        {
                            str(key): {
                                "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                                **(
                                    {
                                        "poll_group": str(
                                            values.get("poll_group", "")
                                        ).strip()
                                    }
                                    if str(values.get("poll_group", "")).strip()
                                    else {}
                                ),
                            }
                            for key, values in device.local_sensor_preferences.items()
                            if isinstance(key, str) and isinstance(values, dict)
                        }
                        if isinstance(device.local_sensor_preferences, dict)
                        else None
                    ),
                }
            )

        return {
            "schema": BACKUP_SCHEMA_NAME,
            "version": BACKUP_SCHEMA_VERSION,
            "exported_by": f"ups2mqtt {APP_VERSION}",
            "exported_at": datetime.now(tz=timezone.utc).isoformat(),
            "devices": exported_devices,
            "profiles": profile_snapshots,
        }

    def _profile_comparable_dict(profile: ProfileConfig) -> dict[str, Any]:
        snapshot = _profile_snapshot_dict(profile)
        return {
            "name": snapshot["name"],
            "driver_key": snapshot["driver_key"],
            "config_payload": snapshot["config_payload"],
            "selected_sensors": snapshot["selected_sensors"],
            "sensor_preferences": snapshot["sensor_preferences"],
            "comments": snapshot["comments"],
            "is_protected": bool(snapshot["is_protected"]),
        }

    def _device_comparable_dict(device: DeviceConfig) -> dict[str, Any]:
        return {
            "device_uid": str(device.device_uid),
            "id": str(device.id),
            "source": str(device.source),
            "host": str(device.host),
            "port": int(device.port),
            "snmp_port": int(device.snmp_port),
            "unit_id": int(device.unit_id),
            "snmp_community": str(device.snmp_community),
            "poll_interval": (
                int(device.poll_interval) if device.poll_interval is not None else None
            ),
            "name": str(device.name or ""),
            "location": str(device.location or ""),
            "debug_logging": bool(device.debug_logging),
            "keep_connection_open": bool(device.keep_connection_open),
            "discovery_enabled": bool(device.discovery_enabled),
            "polling_enabled": bool(device.polling_enabled),
            "profile_uid": str(device.profile_uid or ""),
            "profile_mode": str(device.profile_mode or ""),
            "local_profile_payload": (
                dict(device.local_profile_payload)
                if isinstance(device.local_profile_payload, dict)
                else None
            ),
            "local_selected_sensors": (
                [str(item) for item in device.local_selected_sensors]
                if device.local_selected_sensors is not None
                else None
            ),
            "local_sensor_preferences": (
                {
                    str(key): {
                        "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                        **(
                            {"poll_group": str(values.get("poll_group", "")).strip()}
                            if str(values.get("poll_group", "")).strip()
                            else {}
                        ),
                    }
                    for key, values in device.local_sensor_preferences.items()
                    if isinstance(key, str) and isinstance(values, dict)
                }
                if isinstance(device.local_sensor_preferences, dict)
                else None
            ),
        }

    def _import_devices_from_json_data(json_data: str) -> int:
        raw = json.loads(json_data)
        if not isinstance(raw, dict):
            raise ValueError("Invalid import format: expected root JSON object")
        if "schema" not in raw:
            raise ValueError("Invalid schema: missing 'schema'")
        schema = str(raw.get("schema", "")).strip()
        if schema != BACKUP_SCHEMA_NAME:
            raise ValueError(
                f"Invalid schema: expected '{BACKUP_SCHEMA_NAME}', got '{schema or '(empty)'}'"
            )
        if "version" not in raw:
            raise ValueError("Unsupported version: missing 'version'")
        version_raw = raw.get("version")
        try:
            version = int(version_raw)
        except (TypeError, ValueError):
            raise ValueError(f"Unsupported version: {version_raw}") from None
        if version != BACKUP_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported version: {version} (expected {BACKUP_SCHEMA_VERSION})"
            )
        raw_devices = raw.get("devices", [])
        if not isinstance(raw_devices, list):
            raise ValueError("Invalid schema: 'devices' must be an array")
        raw_profiles = raw.get("profiles", [])
        if not isinstance(raw_profiles, list):
            raise ValueError("Invalid schema: 'profiles' must be an array")

        db = _profile_db()
        if db is None or not hasattr(db, "load_profiles") or not hasattr(
            db, "save_profile"
        ):
            raise ValueError("Profile storage is not available")

        existing_profiles = [item for item in db.load_profiles() if item.profile_uid]
        existing_profiles_by_uid = {item.profile_uid: item for item in existing_profiles}
        existing_profiles_by_name_driver = {
            (item.name.lower(), item.driver_key): item for item in existing_profiles
        }

        imported_profile_snapshots: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(raw_profiles, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Invalid profile entry at index {index}: expected object")
            profile_uid = str(item.get("profile_uid", "")).strip()
            profile_name = str(item.get("name", "")).strip()
            driver_key = str(item.get("driver_key", "")).strip()
            config_payload = item.get("config_payload", {})
            selected_sensors = item.get("selected_sensors", [])
            sensor_preferences = item.get("sensor_preferences", {})
            comments = str(item.get("comments", "") or "")
            is_protected = bool(item.get("is_protected", False))
            if not profile_uid:
                raise ValueError(
                    f"Invalid profile entry at index {index}: missing profile_uid"
                )
            if not profile_name or not driver_key:
                raise ValueError(
                    f"Invalid profile entry at index {index}: missing name/driver_key"
                )
            if not isinstance(config_payload, dict):
                raise ValueError(
                    f"Invalid profile entry at index {index}: config_payload must be object"
                )
            if not isinstance(selected_sensors, list):
                raise ValueError(
                    f"Invalid profile entry at index {index}: selected_sensors must be array"
                )
            if not isinstance(sensor_preferences, dict):
                raise ValueError(
                    f"Invalid profile entry at index {index}: sensor_preferences must be object"
                )
            normalized_snapshot = {
                "profile_uid": profile_uid,
                "name": profile_name,
                "driver_key": driver_key,
                "config_payload": dict(config_payload),
                "selected_sensors": [str(value) for value in selected_sensors],
                "sensor_preferences": {
                    str(key): {
                        "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                        **(
                            {"poll_group": str(values.get("poll_group", "")).strip()}
                            if isinstance(values, dict)
                            and str(values.get("poll_group", "")).strip()
                            else {}
                        ),
                    }
                    for key, values in sensor_preferences.items()
                    if isinstance(key, str) and isinstance(values, dict)
                },
                "comments": comments,
                "is_protected": is_protected,
            }
            previous = imported_profile_snapshots.get(profile_uid)
            if previous is not None and previous != normalized_snapshot:
                raise ValueError(
                    f"Profile UID conflict within import payload: {profile_uid}"
                )
            imported_profile_snapshots[profile_uid] = normalized_snapshot

        resolved_profile_uid_by_import_uid: dict[str, str] = {}
        profiles_to_create: list[ProfileConfig] = []
        staged_existing_by_name_driver = dict(existing_profiles_by_name_driver)
        for import_uid, snapshot in imported_profile_snapshots.items():
            existing_by_uid = existing_profiles_by_uid.get(import_uid)
            if existing_by_uid is not None:
                imported_profile = ProfileConfig(
                    profile_uid=import_uid,
                    name=str(snapshot["name"]),
                    driver_key=str(snapshot["driver_key"]),
                    config_payload=dict(snapshot["config_payload"]),
                    selected_sensors=list(snapshot["selected_sensors"]),
                    sensor_preferences=dict(snapshot["sensor_preferences"]),
                    comments=str(snapshot["comments"]),
                    is_protected=bool(snapshot["is_protected"]),
                )
                if _profile_comparable_dict(existing_by_uid) != _profile_comparable_dict(
                    imported_profile
                ):
                    raise ValueError(
                        f"Profile UID conflict for {import_uid}: existing profile content differs"
                    )
                resolved_profile_uid_by_import_uid[import_uid] = existing_by_uid.profile_uid
                continue
            name_driver_key = (
                str(snapshot["name"]).lower(),
                str(snapshot["driver_key"]),
            )
            existing_by_name = staged_existing_by_name_driver.get(name_driver_key)
            if existing_by_name is not None:
                imported_profile = ProfileConfig(
                    profile_uid=import_uid,
                    name=str(snapshot["name"]),
                    driver_key=str(snapshot["driver_key"]),
                    config_payload=dict(snapshot["config_payload"]),
                    selected_sensors=list(snapshot["selected_sensors"]),
                    sensor_preferences=dict(snapshot["sensor_preferences"]),
                    comments=str(snapshot["comments"]),
                    is_protected=bool(snapshot["is_protected"]),
                )
                if _profile_comparable_dict(existing_by_name) != _profile_comparable_dict(
                    imported_profile
                ):
                    raise ValueError(
                        "Profile conflict for "
                        f"{snapshot['name']} ({snapshot['driver_key']}): existing content differs"
                    )
                resolved_profile_uid_by_import_uid[import_uid] = existing_by_name.profile_uid
                continue
            created = ProfileConfig(
                profile_uid=import_uid,
                name=str(snapshot["name"]),
                driver_key=str(snapshot["driver_key"]),
                config_payload=dict(snapshot["config_payload"]),
                selected_sensors=list(snapshot["selected_sensors"]),
                sensor_preferences=dict(snapshot["sensor_preferences"]),
                comments=str(snapshot["comments"]),
                is_protected=bool(snapshot["is_protected"]),
            )
            profiles_to_create.append(created)
            resolved_profile_uid_by_import_uid[import_uid] = import_uid
            staged_existing_by_name_driver[(created.name.lower(), created.driver_key)] = (
                created
            )

        existing_devices = store.list_devices()
        existing_devices_by_uid = {
            item.device_uid: item for item in existing_devices if item.device_uid
        }
        existing_uid_by_id = {item.id: item.device_uid for item in existing_devices}

        devices_to_upsert: list[DeviceConfig] = []
        skipped_equal = 0
        eligible_drivers = _eligible_profile_drivers()
        for index, item in enumerate(raw_devices, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Invalid device entry at index {index}: expected object")
            device_uid = str(item.get("device_uid", "")).strip()
            if not device_uid:
                raise ValueError(
                    f"Invalid device entry at index {index}: missing device_uid"
                )
            driver_key = str(item.get("driver_key", "")).strip()
            if not driver_key:
                raise ValueError(
                    f"Invalid device entry at index {index}: missing driver_key"
                )
            if driver_key not in eligible_drivers:
                raise ValueError(
                    f"Invalid device entry at index {index}: unsupported driver_key {driver_key}"
                )
            raw_mode = str(item.get("profile_mode", "")).strip().lower()
            profile_mode = (
                raw_mode if raw_mode in {"global", "local", "default"} else "default"
            )
            config_payload = item.get("config", {})
            if not isinstance(config_payload, dict):
                raise ValueError(
                    f"Invalid device entry at index {index}: config must be an object"
                )
            device_id = str(config_payload.get("id", "")).strip()
            host = str(config_payload.get("host", "")).strip()
            if not device_id or not host:
                raise ValueError(
                    f"Invalid device entry at index {index}: config.id and config.host are required"
                )
            try:
                port = int(config_payload.get("port", 502))
                snmp_port = int(config_payload.get("snmp_port", 161))
                unit_id = int(config_payload.get("unit_id", 1))
            except (TypeError, ValueError):
                raise ValueError(
                    f"Invalid device entry at index {index}: port/snmp_port/unit_id must be numeric"
                ) from None
            poll_interval_raw = config_payload.get("poll_interval")
            poll_interval: int | None
            if poll_interval_raw is None or str(poll_interval_raw).strip() == "":
                poll_interval = None
            else:
                try:
                    poll_interval = int(poll_interval_raw)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"Invalid device entry at index {index}: poll_interval must be numeric or null"
                    ) from None

            def _to_bool(value: Any, *, default: bool) -> bool:
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

            profile_uid_raw = item.get("profile_uid")
            profile_uid_text = (
                str(profile_uid_raw).strip() if profile_uid_raw is not None else ""
            )
            profile_name_text = str(item.get("profile_name", "")).strip()
            resolved_profile_uid = ""
            if profile_mode in {"global", "local"}:
                if profile_uid_text:
                    resolved_profile_uid = resolved_profile_uid_by_import_uid.get(
                        profile_uid_text, ""
                    )
                    if not resolved_profile_uid and profile_uid_text in existing_profiles_by_uid:
                        resolved_profile_uid = profile_uid_text
                elif profile_name_text:
                    matched = staged_existing_by_name_driver.get(
                        (profile_name_text.lower(), driver_key)
                    )
                    if matched is not None:
                        resolved_profile_uid = matched.profile_uid
                if profile_mode == "global" and not resolved_profile_uid:
                    raise ValueError(
                        "Profile reference missing for global device "
                        f"{device_id}: profile_uid={profile_uid_text or '(none)'}"
                    )
                if resolved_profile_uid:
                    matched_profile = None
                    for item_profile in profiles_to_create:
                        if item_profile.profile_uid == resolved_profile_uid:
                            matched_profile = item_profile
                            break
                    if matched_profile is None:
                        matched_profile = existing_profiles_by_uid.get(resolved_profile_uid)
                    if (
                        matched_profile is not None
                        and str(matched_profile.driver_key) != driver_key
                    ):
                        raise ValueError(
                            f"Driver/profile mismatch for {device_id}: device driver {driver_key} "
                            f"!= profile driver {matched_profile.driver_key}"
                        )
            else:
                resolved_profile_uid = ""

            local_profile_payload = item.get("local_profile_payload")
            if local_profile_payload is not None and not isinstance(
                local_profile_payload, dict
            ):
                raise ValueError(
                    f"Invalid device entry at index {index}: local_profile_payload must be object or null"
                )
            local_selected_sensors = item.get("local_selected_sensors")
            if local_selected_sensors is not None and not isinstance(
                local_selected_sensors, list
            ):
                raise ValueError(
                    f"Invalid device entry at index {index}: local_selected_sensors must be array or null"
                )
            local_sensor_preferences = item.get("local_sensor_preferences")
            if local_sensor_preferences is not None and not isinstance(
                local_sensor_preferences, dict
            ):
                raise ValueError(
                    f"Invalid device entry at index {index}: local_sensor_preferences must be object or null"
                )

            normalized_local_preferences: dict[str, dict[str, Any]] | None = None
            if isinstance(local_sensor_preferences, dict):
                normalized_local_preferences = {
                    str(key): {
                        "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                        **(
                            {"poll_group": str(values.get("poll_group", "")).strip()}
                            if isinstance(values, dict)
                            and str(values.get("poll_group", "")).strip()
                            else {}
                        ),
                    }
                    for key, values in local_sensor_preferences.items()
                    if isinstance(key, str) and isinstance(values, dict)
                }

            imported_device = DeviceConfig(
                id=device_id,
                source=driver_key,
                host=host,
                port=port,
                snmp_port=snmp_port,
                unit_id=unit_id,
                snmp_community=str(config_payload.get("snmp_community", "public") or "public"),
                poll_interval=poll_interval,
                name=str(item.get("name", "") or "").strip() or None,
                location=str(item.get("location", "") or "").strip() or None,
                debug_logging=_to_bool(config_payload.get("debug_logging"), default=False),
                keep_connection_open=_to_bool(
                    config_payload.get("keep_connection_open"), default=False
                ),
                device_uid=device_uid,
                discovery_enabled=_to_bool(
                    config_payload.get("discovery_enabled"), default=True
                ),
                polling_enabled=_to_bool(
                    config_payload.get("polling_enabled"), default=True
                ),
                profile_uid=resolved_profile_uid if profile_mode != "default" else "",
                profile_mode=profile_mode,
                local_profile_payload=(
                    dict(local_profile_payload)
                    if profile_mode == "local" and isinstance(local_profile_payload, dict)
                    else None
                ),
                local_selected_sensors=(
                    [str(value) for value in local_selected_sensors]
                    if profile_mode == "local" and isinstance(local_selected_sensors, list)
                    else None
                ),
                local_sensor_preferences=(
                    normalized_local_preferences if profile_mode == "local" else None
                ),
            )

            existing = existing_devices_by_uid.get(device_uid)
            if existing is not None:
                if _device_comparable_dict(existing) == _device_comparable_dict(
                    imported_device
                ):
                    skipped_equal += 1
                    continue
                raise ValueError(
                    f"Device UID conflict for {device_uid}: existing device differs"
                )

            existing_uid_for_id = existing_uid_by_id.get(device_id)
            if existing_uid_for_id and existing_uid_for_id != device_uid:
                raise ValueError(
                    f"Device ID conflict for {device_id}: already owned by UID {existing_uid_for_id}"
                )
            devices_to_upsert.append(imported_device)

        with db.transaction():
            if profiles_to_create and hasattr(db, "save_profiles_bulk"):
                db.save_profiles_bulk(profiles_to_create)
                for profile in profiles_to_create:
                    existing_profiles_by_uid[profile.profile_uid] = profile
            else:
                for profile in profiles_to_create:
                    db.save_profile(profile)
                    existing_profiles_by_uid[profile.profile_uid] = profile

            for device in devices_to_upsert:
                store.upsert(device)

        if profiles_to_create or devices_to_upsert:
            trigger_reload()

        return len(devices_to_upsert) + skipped_equal

    def _hx_trigger_payload(
        *,
        refresh_devices: bool = False,
        close_modal: bool = False,
        toast_level: str | None = None,
        toast_message: str | None = None,
    ) -> str:
        payload: dict[str, bool | dict[str, str]] = {}
        if refresh_devices:
            payload["devices-refresh"] = True
        if close_modal:
            payload["close-device-modal"] = True
        if toast_level and toast_message:
            payload["ui-toast"] = {"level": toast_level, "message": toast_message}
        return json.dumps(payload)

    def _clear_metrics_scope(
        *,
        device_uid: str | None = None,
    ) -> tuple[bool, str]:
        target_uid = (device_uid or "").strip()
        if target_uid:
            if trigger_metrics_drop is None:
                return False, "Metrics clear is not available"
            target_device = next(
                (
                    device
                    for device in store.list_devices()
                    if (device.device_uid or device.id) == target_uid
                ),
                None,
            )
            if target_device is not None:
                target_identity = target_device.device_uid or target_device.id
                trigger_metrics_drop(target_identity)
                if target_device.id != target_identity:
                    trigger_metrics_drop(target_device.id)
                return (
                    True,
                    f"Cleared metrics for {target_device.name or target_device.id}",
                )
            trigger_metrics_drop(target_uid)
            return True, f"Cleared metrics for {target_uid}"

        if trigger_metrics_clear is not None:
            trigger_metrics_clear()
            return True, "All metrics cleared"

        if trigger_metrics_drop is None:
            return False, "Metrics clear is not available"

        snapshot = get_metrics_snapshot()
        for metric_key in dict(snapshot.get("devices", {})).keys():
            trigger_metrics_drop(str(metric_key))
        return True, "All metrics cleared"

    def _profile_rows_for_device_form() -> list[dict[str, str | bool]]:
        eligible_drivers = set(_eligible_profile_drivers().keys())
        rows: list[dict[str, str | bool]] = []
        for item in sorted(_load_profiles(), key=lambda profile: profile.name.lower()):
            if item.driver_key not in eligible_drivers:
                continue
            rows.append(
                {
                    "profile_uid": item.profile_uid,
                    "name": item.name,
                    "driver_key": item.driver_key,
                    "is_protected": bool(item.is_protected),
                }
            )
        return rows

    def _profile_by_uid_for_device_form() -> dict[str, ProfileConfig]:
        return {item.profile_uid: item for item in _load_profiles() if item.profile_uid}

    def _infer_profile_uid_from_source(source: str) -> str:
        lookup = _profile_by_uid_for_device_form()
        for item in lookup.values():
            if item.driver_key == source:
                return item.profile_uid
        return ""

    def _device_form_defaults() -> dict[str, object]:
        profile_rows = _profile_rows_for_device_form()
        default_profile_uid = (
            str(profile_rows[0]["profile_uid"]) if profile_rows else ""
        )
        return {
            "id": "",
            "source": "",
            "profile_uid": default_profile_uid,
            "profile_mode": "global" if default_profile_uid else "local",
            "host": "",
            "port": "502",
            "snmp_port": "161",
            "unit_id": "1",
            "snmp_community": "public",
            "poll_interval": "",
            "name": "",
            "location": "",
            "debug_logging": False,
            "keep_connection_open": False,
            "discovery_enabled": True,
            "polling_enabled": True,
            "original_id": "",
            "device_uid": "",
            "_local_profile_payload": None,
            "_local_selected_sensors": None,
            "_local_sensor_preferences": None,
        }

    def _device_form_values_from_device(device: DeviceConfig) -> dict[str, object]:
        inferred_profile_uid = device.profile_uid or _infer_profile_uid_from_source(
            device.source
        )
        return {
            "id": device.id,
            "source": device.source,
            "profile_uid": inferred_profile_uid,
            "profile_mode": (
                device.profile_mode
                if device.profile_mode in {"global", "local"}
                else "local"
            ),
            "host": device.host,
            "port": str(device.port),
            "snmp_port": str(device.snmp_port),
            "unit_id": str(device.unit_id),
            "snmp_community": device.snmp_community,
            "poll_interval": (
                str(device.poll_interval) if device.poll_interval is not None else ""
            ),
            "name": device.name or "",
            "location": device.location or "",
            "debug_logging": bool(device.debug_logging),
            "keep_connection_open": bool(device.keep_connection_open),
            "discovery_enabled": bool(device.discovery_enabled),
            "polling_enabled": bool(device.polling_enabled),
            "original_id": device.id,
            "device_uid": device.device_uid,
            "_local_profile_payload": (
                dict(device.local_profile_payload)
                if isinstance(device.local_profile_payload, dict)
                else None
            ),
            "_local_selected_sensors": (
                [str(item) for item in device.local_selected_sensors]
                if device.local_selected_sensors is not None
                else None
            ),
            "_local_sensor_preferences": (
                {
                    str(key): {
                        "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                        **(
                            {"poll_group": str(values.get("poll_group", "")).strip()}
                            if str(values.get("poll_group", "")).strip()
                            else {}
                        ),
                    }
                    for key, values in device.local_sensor_preferences.items()
                    if isinstance(key, str) and isinstance(values, dict)
                }
                if isinstance(device.local_sensor_preferences, dict)
                else None
            ),
        }

    def _device_form_values_from_post(
        data: dict[str, list[str]],
    ) -> dict[str, object]:
        values = _device_form_defaults()
        values.update(
            {
                "id": (data.get("id", [""])[0]).strip(),
                "source": (data.get("source", [str(values["source"])])[0]).strip(),
                "profile_uid": (data.get("profile_uid", [""])[0]).strip(),
                "profile_mode": (data.get("profile_mode", ["local"])[0]).strip()
                or "local",
                "host": (data.get("host", [""])[0]).strip(),
                "port": (data.get("port", [str(values["port"])])[0]).strip(),
                "snmp_port": (
                    data.get("snmp_port", [str(values["snmp_port"])])[0]
                ).strip(),
                "unit_id": (data.get("unit_id", [str(values["unit_id"])])[0]).strip(),
                "snmp_community": (
                    data.get("snmp_community", [str(values["snmp_community"])])[0]
                ).strip(),
                "poll_interval": (data.get("poll_interval", [""])[0]).strip(),
                "name": (data.get("name", [""])[0]).strip(),
                "location": (data.get("location", [""])[0]).strip(),
                "debug_logging": _bool_from_form(data, "debug_logging"),
                "keep_connection_open": _bool_from_form(data, "keep_connection_open"),
                "discovery_enabled": _bool_from_form(data, "discovery_enabled"),
                "polling_enabled": _bool_from_form(data, "polling_enabled"),
                "original_id": (data.get("original_id", [""])[0]).strip(),
                "device_uid": (data.get("device_uid", [""])[0]).strip(),
            }
        )
        posted_values = {
            key: item[0] for key, item in data.items() if item and isinstance(key, str)
        }
        payload, selected_sensors, sensor_preferences = (
            _profile_editor_overrides_from_values(posted_values)
        )
        values["_local_profile_payload"] = payload
        values["_local_selected_sensors"] = selected_sensors
        values["_local_sensor_preferences"] = sensor_preferences
        return values

    def _build_device_profile_context(
        *,
        form_values: dict[str, object],
        post_data: dict[str, list[str]] | None = None,
    ) -> dict[str, object]:
        profile_rows = _profile_rows_for_device_form()
        profile_by_uid = _profile_by_uid_for_device_form()
        selected_profile_uid = str(form_values.get("profile_uid", "") or "").strip()
        source_fallback = str(form_values.get("source", "") or "").strip()
        if not selected_profile_uid and source_fallback:
            selected_profile_uid = _infer_profile_uid_from_source(source_fallback)
        selected_profile = profile_by_uid.get(selected_profile_uid)
        profile_mode = str(form_values.get("profile_mode", "local") or "local")
        if profile_mode not in {"global", "local"}:
            profile_mode = "local"

        driver_key = (
            selected_profile.driver_key if selected_profile else source_fallback
        )
        contract_profile = _eligible_profile_drivers().get(driver_key)
        selected_profile_missing = bool(
            selected_profile_uid and selected_profile is None
        )
        profile_driver_ineligible = bool(
            selected_profile is not None and contract_profile is None
        )
        local_mode = profile_mode == "local"

        base_payload: dict[str, object] = {}
        base_selected: list[str] = []
        base_preferences: dict[str, dict[str, Any]] = {}
        if selected_profile is not None:
            if isinstance(selected_profile.config_payload, dict):
                base_payload = {
                    str(key): value
                    for key, value in selected_profile.config_payload.items()
                }
            base_selected = [str(item) for item in selected_profile.selected_sensors]
            if isinstance(selected_profile.sensor_preferences, dict):
                base_preferences = {
                    str(key): {
                        "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                        **(
                            {"poll_group": str(values.get("poll_group", "")).strip()}
                            if str(values.get("poll_group", "")).strip()
                            else {}
                        ),
                    }
                    for key, values in selected_profile.sensor_preferences.items()
                    if isinstance(key, str) and isinstance(values, dict)
                }

        if local_mode:
            stored_local_payload = form_values.get("_local_profile_payload")
            if isinstance(stored_local_payload, dict):
                base_payload = {
                    str(key): value for key, value in stored_local_payload.items()
                }
            stored_local_selected = form_values.get("_local_selected_sensors")
            if isinstance(stored_local_selected, list):
                base_selected = [
                    str(item) for item in stored_local_selected if str(item)
                ]
            stored_local_preferences = form_values.get("_local_sensor_preferences")
            if isinstance(stored_local_preferences, dict):
                base_preferences = {
                    str(key): {
                        "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                        **(
                            {"poll_group": str(values.get("poll_group", "")).strip()}
                            if str(values.get("poll_group", "")).strip()
                            else {}
                        ),
                    }
                    for key, values in stored_local_preferences.items()
                    if isinstance(key, str) and isinstance(values, dict)
                }

        if post_data is not None and local_mode:
            posted_values = {
                key: values[0]
                for key, values in post_data.items()
                if values and isinstance(key, str)
            }
            posted_payload, posted_selected, posted_preferences = (
                _profile_editor_overrides_from_values(posted_values)
            )
            if posted_payload:
                base_payload = posted_payload
            has_posted_sensor_keys = any(
                isinstance(key, str)
                and (
                    key.startswith("sensor__")
                    or key.startswith("sensor_key__")
                    or key.startswith("sensor_mqtt__")
                    or key.startswith("sensor_poll_group__")
                )
                for key in post_data
            )
            if has_posted_sensor_keys:
                base_selected = posted_selected
                base_preferences = posted_preferences
                form_values["_local_sensor_preferences"] = posted_preferences

        poll_group_rows: list[tuple[str, int]] = []
        key_precedence_rows: list[tuple[str, str]] = []
        sensor_poll_group_choices: list[str] = []
        sensor_rows: list[dict[str, object]] = []
        profile_catalog_error = ""
        if contract_profile is not None:
            defaults_payload = _profile_default_payload(driver_key, contract_profile)
            sensor_poll_group_choices = sorted(
                str(name)
                for name in dict(defaults_payload.get("poll_groups", {}))
                if str(name).strip()
            )
            allowed_poll_groups = set(sensor_poll_group_choices)
            sensor_poll_group_defaults = _sensor_poll_group_defaults_from_profile(
                contract_profile
            )
            merged_payload = {
                "poll_groups": dict(defaults_payload.get("poll_groups", {})),
                "key_precedence": dict(defaults_payload.get("key_precedence", {})),
            }
            incoming_groups = (
                base_payload.get("poll_groups", {})
                if isinstance(base_payload, dict)
                else {}
            )
            if isinstance(incoming_groups, dict):
                for group_name, interval in incoming_groups.items():
                    if group_name not in merged_payload["poll_groups"]:
                        continue
                    merged_payload["poll_groups"][group_name] = _int_or_default(
                        str(interval), 60
                    )
            incoming_precedence = (
                base_payload.get("key_precedence", {})
                if isinstance(base_payload, dict)
                else {}
            )
            if isinstance(incoming_precedence, dict):
                for metric_key, source_name in incoming_precedence.items():
                    source_text = str(source_name).lower()
                    if metric_key in merged_payload[
                        "key_precedence"
                    ] and source_text in {"modbus", "snmp"}:
                        merged_payload["key_precedence"][metric_key] = source_text
            poll_group_rows = sorted(
                (
                    (str(name), _int_or_default(str(interval), 60))
                    for name, interval in merged_payload["poll_groups"].items()
                ),
                key=lambda item: item[0],
            )
            key_precedence_rows = sorted(
                (
                    (str(name), str(source_name))
                    for name, source_name in merged_payload["key_precedence"].items()
                ),
                key=lambda item: item[0],
            )
            try:
                available_sensor_keys = _profile_allowed_sensor_keys(
                    driver_key,
                    contract_profile,
                )
                selected_set = {str(item) for item in base_selected if str(item)}
                if not base_preferences:
                    base_preferences = _build_sensor_preferences_from_selected(
                        selected_sensors=base_selected,
                        available_keys=available_sensor_keys,
                        default_poll_groups=sensor_poll_group_defaults,
                    )
                allowed_set = set(available_sensor_keys)
                base_preferences = _normalize_sensor_preferences(
                    base_preferences,
                    allowed_keys=allowed_set,
                    allowed_poll_groups=allowed_poll_groups,
                )
                for key in available_sensor_keys:
                    if key not in base_preferences:
                        base_preferences[key] = {
                            "mqtt_enabled": key in selected_set,
                            "poll_group": str(
                                sensor_poll_group_defaults.get(key, "slow")
                            ),
                        }

                apps_dir = str(get_capability_status().get("apps_dir", "/data/apps"))
                catalog_rows = _catalog_sensor_rows_for_driver(
                    apps_dir=apps_dir,
                    driver_key=driver_key,
                )
                if catalog_rows:
                    all_keys: list[str] = []
                    seen_keys: set[str] = set()
                    for item in catalog_rows:
                        key = str(item.get("key", "")).strip()
                        if key and key not in seen_keys:
                            seen_keys.add(key)
                            all_keys.append(key)
                    for key in available_sensor_keys:
                        if key not in seen_keys:
                            seen_keys.add(key)
                            all_keys.append(key)
                    for key in all_keys:
                        if key not in base_preferences:
                            base_preferences[key] = {
                                "mqtt_enabled": False,
                                "poll_group": str(
                                    sensor_poll_group_defaults.get(key, "slow")
                                ),
                            }
                    by_key = {
                        str(item.get("key", "")).strip(): item for item in catalog_rows
                    }
                    for key in all_keys:
                        item = by_key.get(key, {})
                        sensor_rows.append(
                            {
                                "key": key,
                                "mqtt_enabled": bool(
                                    base_preferences.get(key, {}).get(
                                        "mqtt_enabled", False
                                    )
                                ),
                                "category": str(item.get("category", "other")),
                                "label": str(item.get("label", key)),
                                "unit": str(item.get("unit", "")),
                                "source": str(item.get("source", "")),
                                "aliases": str(item.get("aliases", "")),
                                "reference": str(item.get("reference", "")),
                                "from_catalog": key in by_key,
                                "poll_group": str(
                                    base_preferences.get(key, {}).get(
                                        "poll_group",
                                        sensor_poll_group_defaults.get(key, "slow"),
                                    )
                                ),
                            }
                        )
                else:
                    for key in sorted(available_sensor_keys):
                        prefs = base_preferences.get(
                            key,
                        )
                        sensor_rows.append(
                            {
                                "key": key,
                                "mqtt_enabled": bool(prefs.get("mqtt_enabled", True)),
                                "category": "other",
                                "label": key,
                                "unit": "",
                                "source": "",
                                "aliases": "",
                                "reference": "",
                                "from_catalog": False,
                                "poll_group": str(
                                    prefs.get(
                                        "poll_group",
                                        sensor_poll_group_defaults.get(key, "slow"),
                                    )
                                ),
                            }
                        )
            except ValueError as err:
                profile_catalog_error = str(err)

        unified_rows = [
            row for row in sensor_rows if not bool(row.get("from_catalog", False))
        ]
        catalog_groups_map: dict[str, list[dict[str, object]]] = {}
        for row in sensor_rows:
            if not bool(row.get("from_catalog", False)):
                continue
            category = str(row.get("category", "other") or "other")
            catalog_groups_map.setdefault(category, []).append(row)
        catalog_sensor_groups = sorted(
            catalog_groups_map.items(), key=lambda item: _category_sort_key(item[0])
        )

        return {
            "profile_rows": profile_rows,
            "selected_profile_uid": selected_profile_uid,
            "selected_profile_name": selected_profile.name if selected_profile else "",
            "selected_driver_key": driver_key,
            "profile_mode": profile_mode,
            "is_local_mode": local_mode,
            "poll_group_rows": poll_group_rows,
            "key_precedence_rows": key_precedence_rows,
            "sensor_rows": sensor_rows,
            "sensor_rows_are_dual_toggle": driver_key in CATALOG_DRIVER_KEYS,
            "sensor_poll_group_choices": sensor_poll_group_choices,
            "unified_sensor_rows": unified_rows,
            "catalog_sensor_groups": catalog_sensor_groups,
            "has_catalog_sensors": bool(catalog_sensor_groups),
            "profile_select_error": selected_profile_missing,
            "profile_driver_ineligible": profile_driver_ineligible,
            "profile_catalog_error": profile_catalog_error,
        }

    def _build_device_from_post(
        data: dict[str, list[str]],
        *,
        existing_device: DeviceConfig | None = None,
    ) -> DeviceConfig:
        device_id = _validate_device_id((data.get("id", [""])[0]).strip())
        host = _validate_host((data.get("host", [""])[0]).strip())
        port = _validate_port(_int_or_default((data.get("port", [""])[0]), 502))
        snmp_port = _validate_port(
            _int_or_default((data.get("snmp_port", [""])[0]), 161)
        )
        unit_id = _validate_unit_id(_int_or_default((data.get("unit_id", [""])[0]), 1))
        poll_interval_raw = (data.get("poll_interval", [""])[0]).strip()
        poll_interval: int | None = None
        if poll_interval_raw:
            poll_interval = _validate_poll_interval(int(poll_interval_raw))

        profile_uid = (data.get("profile_uid", [""])[0]).strip()
        profile_mode = (data.get("profile_mode", [""])[0]).strip().lower() or "local"
        if profile_mode not in {"global", "local"}:
            raise ValueError("Profile mode must be global or local")

        profile = _get_profile(profile_uid) if profile_uid else None
        if profile_uid and profile is None:
            raise ValueError("Selected profile not found")

        source = (data.get("source", [""])[0]).strip()
        local_profile_payload: dict[str, object] | None = None
        local_selected_sensors: list[str] | None = None
        local_sensor_preferences: dict[str, dict[str, Any]] | None = None

        if profile is not None:
            source = profile.driver_key
            contract_profile = _eligible_profile_drivers().get(source)
            if contract_profile is None:
                raise ValueError(
                    "Selected profile driver is no longer available in live capabilities"
                )
            defaults = _profile_default_payload(source, contract_profile)
            if profile_mode == "local":
                posted_values = {
                    key: item[0]
                    for key, item in data.items()
                    if item and isinstance(key, str)
                }
                posted_payload, posted_selected, posted_preferences = (
                    _profile_editor_overrides_from_values(posted_values)
                )
                profile_payload = (
                    profile.config_payload
                    if isinstance(profile.config_payload, dict)
                    else {}
                )
                local_profile_payload = {
                    "driver_key": source,
                    "poll_groups": dict(defaults.get("poll_groups", {})),
                    "key_precedence": dict(defaults.get("key_precedence", {})),
                }
                for group_name, interval in dict(
                    profile_payload.get("poll_groups", {})
                ).items():
                    if group_name in dict(defaults.get("poll_groups", {})):
                        local_profile_payload["poll_groups"][group_name] = max(
                            1,
                            _int_or_default(str(interval), 60),
                        )
                for metric_key, source_name in dict(
                    profile_payload.get("key_precedence", {})
                ).items():
                    source_text = str(source_name).strip().lower()
                    if metric_key in dict(
                        defaults.get("key_precedence", {})
                    ) and source_text in {"modbus", "snmp"}:
                        local_profile_payload["key_precedence"][metric_key] = (
                            source_text
                        )
                incoming_groups = dict(posted_payload.get("poll_groups", {}))
                for group_name, interval in incoming_groups.items():
                    if group_name not in dict(defaults.get("poll_groups", {})):
                        raise ValueError(f"Unknown poll group: {group_name}")
                    local_profile_payload["poll_groups"][group_name] = max(
                        1,
                        _int_or_default(str(interval), 60),
                    )
                incoming_precedence = dict(posted_payload.get("key_precedence", {}))
                for metric_key, source_name in incoming_precedence.items():
                    source_text = str(source_name).strip().lower()
                    if metric_key not in dict(defaults.get("key_precedence", {})):
                        raise ValueError(f"Unknown key precedence key: {metric_key}")
                    if source_text not in {"modbus", "snmp"}:
                        raise ValueError(
                            f"Invalid key precedence value for {metric_key}: {source_name}"
                        )
                    local_profile_payload["key_precedence"][metric_key] = source_text
                available_sensor_keys = set(
                    _profile_allowed_sensor_keys(
                        source,
                        contract_profile,
                    )
                )
                poll_group_defaults = _sensor_poll_group_defaults_from_profile(
                    contract_profile
                )
                allowed_poll_groups = set(dict(defaults.get("poll_groups", {})))
                invalid_sensors = sorted(
                    key for key in posted_selected if key not in available_sensor_keys
                )
                if invalid_sensors:
                    raise ValueError(
                        f"Unknown sensors for driver {source}: {', '.join(invalid_sensors)}"
                    )
                invalid_sensor_preferences = sorted(
                    key
                    for key in posted_preferences
                    if key not in available_sensor_keys
                )
                if invalid_sensor_preferences:
                    raise ValueError(
                        "Unknown sensor preferences for driver "
                        f"{source}: {', '.join(invalid_sensor_preferences)}"
                    )
                has_posted_sensor_keys = any(
                    isinstance(key, str)
                    and (
                        key.startswith("sensor__")
                        or key.startswith("sensor_key__")
                        or key.startswith("sensor_mqtt__")
                        or key.startswith("sensor_poll_group__")
                    )
                    for key in data
                )
                if has_posted_sensor_keys:
                    local_selected_sensors = sorted(set(posted_selected))
                    local_sensor_preferences = _normalize_sensor_preferences(
                        posted_preferences,
                        allowed_keys=available_sensor_keys,
                        allowed_poll_groups=allowed_poll_groups,
                    )
                    if not local_sensor_preferences:
                        local_sensor_preferences = (
                            _build_sensor_preferences_from_selected(
                                selected_sensors=local_selected_sensors,
                                available_keys=sorted(available_sensor_keys),
                                default_poll_groups=poll_group_defaults,
                            )
                        )
                else:
                    local_selected_sensors = [
                        key
                        for key in profile.selected_sensors
                        if key in available_sensor_keys
                    ]
                    local_sensor_preferences = _normalize_sensor_preferences(
                        (
                            profile.sensor_preferences
                            if isinstance(profile.sensor_preferences, dict)
                            else None
                        ),
                        allowed_keys=available_sensor_keys,
                        allowed_poll_groups=allowed_poll_groups,
                    )
                    if not local_sensor_preferences:
                        local_sensor_preferences = (
                            _build_sensor_preferences_from_selected(
                                selected_sensors=local_selected_sensors,
                                available_keys=sorted(available_sensor_keys),
                                default_poll_groups=poll_group_defaults,
                            )
                        )
            else:
                local_profile_payload = None
                local_selected_sensors = None
                local_sensor_preferences = None
        else:
            profile_mode = "local"
            if not source:
                if existing_device is not None and existing_device.source:
                    source = existing_device.source
                else:
                    raise ValueError("Profile is required")

        return DeviceConfig(
            id=device_id,
            source=source,
            host=host,
            port=port,
            snmp_port=snmp_port,
            unit_id=unit_id,
            snmp_community=(data.get("snmp_community", ["public"])[0]).strip()
            or "public",
            poll_interval=poll_interval,
            name=(data.get("name", [""])[0]).strip() or None,
            location=(data.get("location", [""])[0]).strip() or None,
            debug_logging=_bool_from_form(data, "debug_logging"),
            keep_connection_open=_bool_from_form(data, "keep_connection_open"),
            device_uid=(data.get("device_uid", [""])[0]).strip(),
            discovery_enabled=_bool_from_form(data, "discovery_enabled"),
            polling_enabled=_bool_from_form(data, "polling_enabled"),
            profile_uid=profile.profile_uid if profile is not None else "",
            profile_mode=profile_mode,
            local_profile_payload=local_profile_payload,
            local_selected_sensors=local_selected_sensors,
            local_sensor_preferences=local_sensor_preferences,
        )

    def _render_htmx_device_modal(
        mode: str,
        form_values: dict[str, object] | None = None,
        error_message: str = "",
        post_data: dict[str, list[str]] | None = None,
    ) -> str:
        values = form_values or _device_form_defaults()
        profile_context = _build_device_profile_context(
            form_values=values,
            post_data=post_data,
        )
        return templates.get_template("htmx/device_modal.html").render(
            mode=mode,
            title="Edit Device" if mode == "edit" else "Add Device",
            form=values,
            error_message=error_message,
            **profile_context,
        )

    def _profile_editor_overrides_from_values(
        values: dict[str, str],
    ) -> tuple[dict[str, object], list[str], dict[str, dict[str, Any]]]:
        payload: dict[str, object] = {
            "poll_groups": {},
            "key_precedence": {},
        }
        selected_sensors: list[str] = []
        sensor_keys: set[str] = set()
        mqtt_enabled: set[str] = set()
        poll_group_overrides: dict[str, str] = {}
        for key, value in values.items():
            if key.startswith("cfg_poll_group__"):
                group = key.removeprefix("cfg_poll_group__").strip()
                if group:
                    payload["poll_groups"][group] = _int_or_default(value, 60)
            elif key.startswith("cfg_key_precedence__"):
                metric = key.removeprefix("cfg_key_precedence__").strip()
                option = value.strip().lower()
                if metric and option in {"modbus", "snmp"}:
                    payload["key_precedence"][metric] = option
            elif key.startswith("sensor__"):
                sensor_key = key.removeprefix("sensor__").strip()
                if sensor_key and not _is_bitfield_sensor_key(sensor_key):
                    selected_sensors.append(sensor_key)
            elif key.startswith("sensor_key__"):
                sensor_key = key.removeprefix("sensor_key__").strip()
                if sensor_key and not _is_bitfield_sensor_key(sensor_key):
                    sensor_keys.add(sensor_key)
            elif key.startswith("sensor_mqtt__"):
                sensor_key = key.removeprefix("sensor_mqtt__").strip()
                if sensor_key and not _is_bitfield_sensor_key(sensor_key):
                    mqtt_enabled.add(sensor_key)
            elif key.startswith("sensor_poll_group__"):
                sensor_key = key.removeprefix("sensor_poll_group__").strip()
                poll_group = value.strip()
                if (
                    sensor_key
                    and poll_group
                    and not _is_bitfield_sensor_key(sensor_key)
                ):
                    poll_group_overrides[sensor_key] = poll_group
        sensor_preferences: dict[str, dict[str, Any]] = {}
        if sensor_keys:
            for key in sorted(sensor_keys):
                sensor_preferences[key] = {
                    "mqtt_enabled": key in mqtt_enabled,
                    **(
                        {"poll_group": poll_group_overrides[key]}
                        if key in poll_group_overrides
                        else {}
                    ),
                }
            selected_sensors = [
                key for key in sorted(sensor_keys) if key in mqtt_enabled
            ]
        return payload, selected_sensors, sensor_preferences

    def _build_profile_from_post(
        data: dict[str, list[str]], *, force_create: bool = False
    ) -> ProfileConfig:
        posted_profile_uid = (data.get("profile_uid", [""])[0]).strip()
        profile_uid = (
            str(uuid4()) if force_create else (posted_profile_uid or str(uuid4()))
        )
        profile_name = (data.get("profile_name", [""])[0]).strip()
        driver_key = (data.get("driver_key", [""])[0]).strip()
        comments = (data.get("comments", [""])[0]).strip()

        if not profile_name:
            raise ValueError("Profile name is required")
        if len(profile_name) > 120:
            raise ValueError("Profile name must be 120 characters or fewer")

        existing = _load_profiles()
        existing_profile = next(
            (item for item in existing if item.profile_uid == profile_uid), None
        )
        protection_disabled = ProfileDatabase._is_profile_protection_disabled()
        if (
            existing_profile is not None
            and existing_profile.is_protected
            and not protection_disabled
        ):
            raise ValueError(
                "This is a default protected profile and cannot be edited."
            )
        eligible = _eligible_profile_drivers()
        if existing_profile is not None:
            stored_contract = eligible.get(existing_profile.driver_key)
            stored_driver_eligible = _is_profile_driver_eligible(
                existing_profile.driver_key,
                stored_contract,
            )
            if not stored_driver_eligible:
                raise ValueError(
                    "This profile's stored driver is no longer available in live capabilities. "
                    "Create a new profile using a supported driver."
                )

        contract_profile = eligible.get(driver_key)
        if contract_profile is None or not _is_profile_driver_eligible(
            driver_key, contract_profile
        ):
            raise ValueError(f"Driver {driver_key} is not eligible for profiles")

        for item in existing:
            if item.profile_uid == profile_uid:
                continue
            if item.name.lower() == profile_name.lower():
                raise ValueError(f"Profile name {profile_name} already exists")

        posted_values = {
            key: values[0]
            for key, values in data.items()
            if values and isinstance(key, str)
        }
        posted_payload, posted_selected, posted_preferences = (
            _profile_editor_overrides_from_values(posted_values)
        )
        defaults = _profile_default_payload(driver_key, contract_profile)
        payload = {
            "driver_key": driver_key,
            "poll_groups": dict(defaults.get("poll_groups", {})),
            "key_precedence": dict(defaults.get("key_precedence", {})),
        }

        for group_name, interval in dict(posted_payload.get("poll_groups", {})).items():
            if group_name not in dict(defaults.get("poll_groups", {})):
                raise ValueError(f"Unknown poll group: {group_name}")
            interval_int = _int_or_default(str(interval), 60)
            if interval_int <= 0:
                raise ValueError(f"Poll group interval must be > 0: {group_name}")
            payload["poll_groups"][group_name] = interval_int

        for key_name, source_name in dict(
            posted_payload.get("key_precedence", {})
        ).items():
            if key_name not in dict(defaults.get("key_precedence", {})):
                raise ValueError(f"Unknown key_precedence key: {key_name}")
            if source_name not in {"modbus", "snmp"}:
                raise ValueError(f"Invalid key precedence value: {source_name}")
            payload["key_precedence"][key_name] = source_name

        sensor_keys = _profile_allowed_sensor_keys(
            driver_key,
            contract_profile,
        )
        poll_group_defaults = _sensor_poll_group_defaults_from_profile(contract_profile)
        allowed_poll_groups = set(dict(defaults.get("poll_groups", {})))
        sensor_set = set(sensor_keys)
        invalid_sensors = sorted(
            {key for key in posted_selected if key not in sensor_set}
        )
        if invalid_sensors:
            raise ValueError(
                f"Unknown sensors for driver {driver_key}: {', '.join(invalid_sensors)}"
            )
        invalid_preference_keys = sorted(
            {key for key in posted_preferences if key not in sensor_set}
        )
        if invalid_preference_keys:
            raise ValueError(
                "Unknown sensor preferences for driver "
                f"{driver_key}: {', '.join(invalid_preference_keys)}"
            )
        selected_sensors = sorted(set(posted_selected))
        normalized_preferences = _normalize_sensor_preferences(
            posted_preferences,
            allowed_keys=sensor_set,
            allowed_poll_groups=allowed_poll_groups,
        )
        if not normalized_preferences:
            normalized_preferences = _build_sensor_preferences_from_selected(
                selected_sensors=selected_sensors,
                available_keys=sensor_keys,
                default_poll_groups=poll_group_defaults,
            )

        return ProfileConfig(
            profile_uid=profile_uid,
            name=profile_name,
            driver_key=driver_key,
            config_payload=payload,
            selected_sensors=selected_sensors,
            sensor_preferences=normalized_preferences,
            comments=comments,
        )

    class Handler(BaseHTTPRequestHandler):
        def _request_base_path(self) -> str:
            ingress_path = (self.headers.get("X-Ingress-Path") or "").strip()
            if ingress_path:
                return _normalize_base_path(ingress_path)
            return normalized_base_path

        def _prefixed_path(self, path: str) -> str:
            if not path.startswith("/"):
                return path
            base_path = self._request_base_path()
            if base_path == "/":
                return path
            return f"{base_path}{path}"

        def _resolve_app_path(self, request_path: str) -> str:
            if request_path in {"", "/"}:
                return "/"
            if request_path.startswith("/htmx/"):
                return request_path
            if "/htmx/" in request_path:
                return request_path[request_path.index("/htmx/") :]
            for suffix in (
                "/metrics.json",
                "/check-config.json",
                "/favicon.ico",
                "/favicon.png",
            ):
                if request_path == suffix or request_path.endswith(suffix):
                    return suffix
            if request_path.endswith("/"):
                return "/"
            return request_path

        def _send_html(
            self,
            payload: str,
            status: HTTPStatus = HTTPStatus.OK,
            headers: dict[str, str] | None = None,
        ) -> None:
            data = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if headers:
                for key, value in headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _handle_htmx_get(self, parsed_path) -> bool:
            params = parse_qs(parsed_path.query)

            if parsed_path.path == "/htmx/devices":
                filters = _device_filter_values_from_params(params)
                payload = templates.get_template("htmx/devices_page.html").render(
                    initial_panel_html=_render_htmx_devices_panel(filters),
                    initial_theme_choice=_normalize_theme(theme_getter()),
                    sidebar_versions=_sidebar_version_items(),
                    web_base_path=self._request_base_path(),
                )
                self._send_html(payload)
                return True

            if parsed_path.path == "/htmx/maintenance/backup/export":
                devices = store.list_devices()
                payload = _device_export_payload(devices)
                payload_json = json.dumps(payload, indent=2, sort_keys=True).encode(
                    "utf-8"
                )
                timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename=ups2mqtt-backup-{timestamp}.json",
                )
                self.send_header("Content-Length", str(len(payload_json)))
                self.end_headers()
                self.wfile.write(payload_json)
                return True

            if parsed_path.path == "/htmx/maintenance/import/template.csv":
                template_csv = _generate_devices_csv_template()
                payload_csv = template_csv.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename=ups2mqtt-import-template.csv",
                )
                self.send_header("Content-Length", str(len(payload_csv)))
                self.end_headers()
                self.wfile.write(payload_csv)
                return True

            if parsed_path.path == "/htmx/devices/partials/panel/devices":
                self._send_html(
                    _render_htmx_devices_panel(
                        _device_filter_values_from_params(params)
                    )
                )
                return True

            if parsed_path.path == "/htmx/devices/partials/panel/metrics":
                self._send_html(_render_htmx_metrics_panel())
                return True

            if parsed_path.path == "/htmx/devices/partials/panel/logs":
                self._send_html(_render_htmx_logs_panel(params))
                return True

            if parsed_path.path == "/htmx/devices/partials/panel/maintenance":
                self._send_html(_render_htmx_maintenance_panel())
                return True

            if parsed_path.path == "/htmx/devices/partials/panel/configuration":
                self._send_html(_render_htmx_configuration_panel())
                return True

            if parsed_path.path == "/htmx/devices/partials/panel/profiles":
                self._send_html(_render_htmx_profiles_panel())
                return True

            if parsed_path.path == "/htmx/devices/partials/table":
                self._send_html(
                    _render_htmx_devices_table(
                        _device_filter_values_from_params(params)
                    )
                )
                return True

            if parsed_path.path == "/htmx/devices/partials/modal/ha-payload":
                device_id = params.get("id", [""])[0].strip()
                device = store.get(device_id) if device_id else None
                payload, status = _render_htmx_device_ha_payload_modal(
                    device=device,
                    not_found_device_id=device_id,
                )
                self._send_html(payload, status=status)
                return True

            if parsed_path.path == "/htmx/devices/partials/modal":
                mode = params.get("mode", ["add"])[0]
                if mode == "edit":
                    has_form_params = bool(params.get("id")) and bool(
                        params.get("host")
                    )
                    if has_form_params:
                        self._send_html(
                            _render_htmx_device_modal(
                                mode="edit",
                                form_values=_device_form_values_from_post(params),
                                post_data=params,
                            )
                        )
                        return True
                    device_id = params.get("id", [""])[0].strip()
                    device = store.get(device_id)
                    if device is None:
                        self._send_html(
                            _render_htmx_device_modal(
                                mode="add",
                                error_message=f"Device {device_id} not found",
                            ),
                            status=HTTPStatus.NOT_FOUND,
                            headers={
                                "HX-Trigger": _hx_trigger_payload(
                                    toast_level="danger",
                                    toast_message=f"Device {device_id} not found",
                                )
                            },
                        )
                        return True
                    self._send_html(
                        _render_htmx_device_modal(
                            mode="edit",
                            form_values=_device_form_values_from_device(device),
                        )
                    )
                    return True
                if params.get("profile_uid") or params.get("host"):
                    self._send_html(
                        _render_htmx_device_modal(
                            mode="add",
                            form_values=_device_form_values_from_post(params),
                            post_data=params,
                        )
                    )
                    return True
                self._send_html(_render_htmx_device_modal(mode="add"))
                return True

            if parsed_path.path == "/htmx/profiles/partials/form":
                profile_uid = params.get("profile_uid", [""])[0].strip()
                profile_name = params.get("profile_name", [""])[0].strip()
                driver_key = params.get("driver_key", [""])[0].strip()
                self._send_html(
                    _render_htmx_profiles_form_for_new(
                        profile_uid=profile_uid,
                        profile_name=profile_name,
                        driver_key=driver_key,
                    )
                )
                return True

            if parsed_path.path == "/htmx/profiles/actions/edit":
                profile_uid = params.get("profile_uid", [""])[0].strip()
                profile = _get_profile(profile_uid)
                if profile is None:
                    self._send_html(
                        _render_htmx_profiles_form(
                            error_message=f"Profile {profile_uid} not found"
                        ),
                        status=HTTPStatus.NOT_FOUND,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=f"Profile {profile_uid} not found",
                            )
                        },
                    )
                    return True
                self._send_html(
                    _render_htmx_profiles_form(
                        profile_uid=profile.profile_uid,
                        profile_name=profile.name,
                        driver_key=profile.driver_key,
                        config_payload=profile.config_payload,
                        selected_sensors=profile.selected_sensors,
                        sensor_preferences=profile.sensor_preferences,
                        comments=profile.comments,
                        is_protected_profile=bool(profile.is_protected)
                        and not ProfileDatabase._is_profile_protection_disabled(),
                        error_message=(
                            "This is a default protected profile and cannot be edited."
                            if profile.is_protected
                            and not ProfileDatabase._is_profile_protection_disabled()
                            else ""
                        ),
                    )
                )
                return True

            if parsed_path.path == "/htmx/profiles/actions/copy":
                profile_uid = params.get("profile_uid", [""])[0].strip()
                profile = _get_profile(profile_uid)
                if profile is None:
                    self._send_html(
                        _render_htmx_profiles_form(
                            error_message=f"Profile {profile_uid} not found"
                        ),
                        status=HTTPStatus.NOT_FOUND,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=f"Profile {profile_uid} not found",
                            )
                        },
                    )
                    return True
                self._send_html(
                    _render_htmx_profiles_form(
                        profile_uid="",
                        profile_name="",
                        driver_key=profile.driver_key,
                        config_payload=profile.config_payload,
                        selected_sensors=profile.selected_sensors,
                        sensor_preferences=profile.sensor_preferences,
                        comments=profile.comments,
                        save_action_path="/htmx/profiles/actions/copy-save",
                        copy_source_name=profile.name,
                    )
                )
                return True

            return False

        def _handle_htmx_post(self, parsed_path, data: dict[str, list[str]]) -> bool:
            filters = _device_filter_values_from_data(data)
            if parsed_path.path == "/htmx/devices/actions/upsert":
                try:
                    original_id = (data.get("original_id", [""])[0]).strip()
                    existing_device = store.get(original_id) if original_id else None
                    device = _build_device_from_post(
                        data, existing_device=existing_device
                    )
                    if (
                        original_id
                        and original_id != device.id
                        and store.get(original_id)
                    ):
                        store.delete(original_id)
                    store.upsert(device)
                    trigger_reload()
                    self._send_html(
                        "",
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                refresh_devices=True,
                                close_modal=True,
                                toast_level="success",
                                toast_message=f"Saved device {device.id}",
                            ),
                        },
                    )
                except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                    mode = (
                        "edit" if (data.get("original_id", [""])[0]).strip() else "add"
                    )
                    self._send_html(
                        _render_htmx_device_modal(
                            mode=mode,
                            form_values=_device_form_values_from_post(data),
                            error_message=str(err),
                            post_data=data,
                        ),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=str(err),
                            )
                        },
                    )
                return True

            if parsed_path.path == "/htmx/maintenance/import/csv":
                csv_data = (data.get("csv_file", [""])[0]).strip()
                if not csv_data:
                    self._send_html(
                        _render_htmx_maintenance_panel(),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message="No CSV file provided",
                            )
                        },
                    )
                    return True
                imported = _import_devices_from_csv_data(csv_data)
                self._send_html(
                    _render_htmx_maintenance_panel(),
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            refresh_devices=True,
                            toast_level="success",
                            toast_message=f"Imported {imported} device(s) from CSV",
                        )
                    },
                )
                return True

            if parsed_path.path == "/htmx/maintenance/backup/import":
                json_data = (data.get("json_file", [""])[0]).strip()
                if not json_data:
                    self._send_html(
                        _render_htmx_maintenance_panel(),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message="No JSON file provided",
                            )
                        },
                    )
                    return True
                try:
                    imported = _import_devices_from_json_data(json_data)
                except (
                    json.JSONDecodeError,
                    ValueError,
                    TypeError,
                    KeyError,
                ) as err:
                    self._send_html(
                        _render_htmx_maintenance_panel(),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=f"JSON import failed: {err}",
                            )
                        },
                    )
                    return True
                self._send_html(
                    _render_htmx_maintenance_panel(),
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            refresh_devices=True,
                            toast_level="success",
                            toast_message=f"Imported {imported} device(s) from JSON",
                        )
                    },
                )
                return True

            if parsed_path.path == "/htmx/devices/actions/toggle":
                device_id = (data.get("id", [""])[0]).strip()
                field = (data.get("field", ["polling_enabled"])[0]).strip()
                current = store.get(device_id)
                if not device_id or current is None:
                    self._send_html(
                        _render_htmx_devices_table(filters),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=f"Device {device_id} not found",
                            )
                        },
                    )
                    return True
                if field == "debug_logging":
                    updated = _clone_device(
                        current,
                        debug_logging=not current.debug_logging,
                    )
                    field_label = "Logging"
                    enabled = updated.debug_logging
                elif field == "keep_connection_open":
                    updated = _clone_device(
                        current,
                        keep_connection_open=not current.keep_connection_open,
                    )
                    field_label = "Keep Conn"
                    enabled = updated.keep_connection_open
                elif field == "discovery_enabled":
                    updated = _clone_device(
                        current,
                        discovery_enabled=not current.discovery_enabled,
                    )
                    field_label = "Discovery"
                    enabled = updated.discovery_enabled
                elif field == "polling_enabled":
                    updated = _clone_device(
                        current,
                        polling_enabled=not current.polling_enabled,
                    )
                    field_label = "Polling"
                    enabled = updated.polling_enabled
                else:
                    self._send_html(
                        _render_htmx_devices_table(filters),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=f"Unsupported toggle field: {field}",
                            )
                        },
                    )
                    return True
                store.upsert(updated)
                trigger_reload()
                self._send_html(
                    _render_htmx_devices_table(filters),
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            toast_level="success",
                            toast_message=f"{field_label} {'enabled' if enabled else 'disabled'} for {device_id}",
                        )
                    },
                )
                return True

            if parsed_path.path == "/htmx/devices/actions/reinitialize":
                device_id = (data.get("id", [""])[0]).strip()
                if not device_id or trigger_device_reinitialize is None:
                    self._send_html(
                        _render_htmx_devices_table(filters),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message="Device reinitialize not available",
                            )
                        },
                    )
                    return True
                current = store.get(device_id)
                if current is None:
                    self._send_html(
                        _render_htmx_devices_table(filters),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=f"Device {device_id} not found",
                            )
                        },
                    )
                    return True
                trigger_device_reinitialize(device_id)
                self._send_html(
                    _render_htmx_devices_table(filters),
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            toast_level="success",
                            toast_message=f"Reinitializing MQTT discovery for {device_id}",
                        )
                    },
                )
                return True

            if parsed_path.path == "/htmx/devices/actions/delete":
                device_id = (data.get("id", [""])[0]).strip()
                current = store.get(device_id)
                if not device_id or current is None or not store.delete(device_id):
                    self._send_html(
                        _render_htmx_devices_table(filters),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=f"Delete failed for {device_id}",
                            )
                        },
                    )
                    return True
                if trigger_metrics_drop:
                    if current.device_uid:
                        trigger_metrics_drop(current.device_uid)
                    trigger_metrics_drop(device_id)
                trigger_reload()
                self._send_html(
                    _render_htmx_devices_table(filters),
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            toast_level="success",
                            toast_message=f"Deleted device {device_id}",
                        )
                    },
                )
                return True

            if parsed_path.path == "/htmx/profiles/actions/upsert":
                db = _profile_db()
                if db is None or not hasattr(db, "save_profile"):
                    self._send_html(
                        _render_htmx_profiles_form(
                            error_message="Profile storage is not available"
                        ),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message="Profile storage is not available",
                            )
                        },
                    )
                    return True
                try:
                    profile = _build_profile_from_post(data)
                    db.save_profile(profile)
                    trigger_reload()
                    self._send_html(
                        _render_htmx_profiles_panel(),
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="success",
                                toast_message=f"Saved profile {profile.name}",
                            )
                        },
                    )
                except (ValueError, TypeError, OSError) as err:
                    profile_uid = (data.get("profile_uid", [""])[0]).strip()
                    profile_name = (data.get("profile_name", [""])[0]).strip()
                    driver_key = (data.get("driver_key", [""])[0]).strip()
                    comments = (data.get("comments", [""])[0]).strip()
                    existing_profile = (
                        _get_profile(profile_uid) if profile_uid else None
                    )
                    posted_values = {
                        key: values[0]
                        for key, values in data.items()
                        if values and isinstance(key, str)
                    }
                    payload, selected, preferences = (
                        _profile_editor_overrides_from_values(posted_values)
                    )
                    self._send_html(
                        _render_htmx_profiles_form(
                            profile_uid=profile_uid,
                            profile_name=profile_name,
                            driver_key=driver_key,
                            config_payload=payload,
                            selected_sensors=selected,
                            sensor_preferences=preferences,
                            comments=comments,
                            error_message=str(err),
                            is_protected_profile=bool(
                                existing_profile.is_protected
                                if existing_profile is not None
                                else False
                            )
                            and not ProfileDatabase._is_profile_protection_disabled(),
                        ),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=str(err),
                            )
                        },
                    )
                return True

            if parsed_path.path == "/htmx/profiles/actions/reinitialize":
                profile_uid = (data.get("profile_uid", [""])[0]).strip()
                profile = _get_profile(profile_uid) if profile_uid else None
                if not profile_uid or profile is None:
                    self._send_html(
                        _render_htmx_profiles_panel(),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=f"Profile {profile_uid} not found",
                            )
                        },
                    )
                    return True
                affected_devices = _devices_bound_to_profile(profile_uid)
                if trigger_device_reinitialize is None:
                    self._send_html(
                        _render_htmx_profiles_panel(),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message="Device reinitialize not available",
                            )
                        },
                    )
                    return True
                success_ids: list[str] = []
                failed_ids: list[str] = []
                skipped_ids: list[str] = []
                for device in affected_devices:
                    device_id = str(device.id or "").strip()
                    if not device_id:
                        skipped_ids.append("(missing-id)")
                        continue
                    try:
                        trigger_device_reinitialize(device_id)
                        success_ids.append(device_id)
                    except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                        failed_ids.append(device_id)
                total = len(affected_devices)
                if total == 0:
                    toast_level = "success"
                    toast_message = f"No affected devices for profile {profile.name}; nothing queued"
                    status_code = HTTPStatus.OK
                elif failed_ids:
                    toast_level = "danger"
                    toast_message = (
                        f"Bulk reinitialize for {profile.name}: queued={len(success_ids)} "
                        f"failed={len(failed_ids)} skipped={len(skipped_ids)}"
                    )
                    status_code = HTTPStatus.BAD_REQUEST
                else:
                    toast_level = "success"
                    toast_message = (
                        f"Bulk reinitialize for {profile.name}: queued={len(success_ids)} "
                        f"failed=0 skipped={len(skipped_ids)}"
                    )
                    status_code = HTTPStatus.OK
                self._send_html(
                    _render_htmx_profiles_panel(),
                    status=status_code,
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            toast_level=toast_level,
                            toast_message=toast_message,
                        )
                    },
                )
                return True

            if parsed_path.path == "/htmx/profiles/actions/copy-save":
                db = _profile_db()
                if db is None or not hasattr(db, "save_profile"):
                    self._send_html(
                        _render_htmx_profiles_form(
                            error_message="Profile storage is not available"
                        ),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message="Profile storage is not available",
                            )
                        },
                    )
                    return True
                try:
                    profile = _build_profile_from_post(data, force_create=True)
                    db.save_profile(profile)
                    trigger_reload()
                    self._send_html(
                        _render_htmx_profiles_panel(),
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="success",
                                toast_message=f"Saved profile {profile.name}",
                            )
                        },
                    )
                except (ValueError, TypeError, OSError) as err:
                    profile_name = (data.get("profile_name", [""])[0]).strip()
                    driver_key = (data.get("driver_key", [""])[0]).strip()
                    comments = (data.get("comments", [""])[0]).strip()
                    source_name = (data.get("copy_source_name", [""])[0]).strip()
                    posted_values = {
                        key: values[0]
                        for key, values in data.items()
                        if values and isinstance(key, str)
                    }
                    payload, selected, preferences = (
                        _profile_editor_overrides_from_values(posted_values)
                    )
                    self._send_html(
                        _render_htmx_profiles_form(
                            profile_uid="",
                            profile_name=profile_name,
                            driver_key=driver_key,
                            config_payload=payload,
                            selected_sensors=selected,
                            sensor_preferences=preferences,
                            comments=comments,
                            error_message=str(err),
                            save_action_path="/htmx/profiles/actions/copy-save",
                            copy_source_name=source_name,
                        ),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=str(err),
                            )
                        },
                    )
                return True

            if parsed_path.path == "/htmx/profiles/actions/delete":
                profile_uid = (data.get("profile_uid", [""])[0]).strip()
                db = _profile_db()
                existing_profile = _get_profile(profile_uid) if profile_uid else None
                if (
                    existing_profile is not None
                    and existing_profile.is_protected
                    and not ProfileDatabase._is_profile_protection_disabled()
                ):
                    self._send_html(
                        _render_htmx_profiles_panel(),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message="This is a default protected profile and cannot be deleted.",
                            )
                        },
                    )
                    return True
                if (
                    not profile_uid
                    or db is None
                    or not hasattr(db, "delete_profile")
                    or not db.delete_profile(profile_uid)
                ):
                    self._send_html(
                        _render_htmx_profiles_panel(),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=f"Delete failed for profile {profile_uid}",
                            )
                        },
                    )
                    return True
                trigger_reload()
                self._send_html(
                    _render_htmx_profiles_panel(),
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            toast_level="success",
                            toast_message="Deleted profile",
                        )
                    },
                )
                return True

            if parsed_path.path == "/htmx/devices/actions/profile/restore_global":
                device_id = (data.get("id", [""])[0]).strip()
                current = store.get(device_id)
                if not device_id or current is None:
                    self._send_html(
                        _render_htmx_devices_table(filters),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message=f"Device {device_id} not found",
                            )
                        },
                    )
                    return True
                if not current.profile_uid:
                    self._send_html(
                        _render_htmx_devices_table(filters),
                        status=HTTPStatus.BAD_REQUEST,
                        headers={
                            "HX-Trigger": _hx_trigger_payload(
                                toast_level="danger",
                                toast_message="Device is not bound to a profile",
                            )
                        },
                    )
                    return True
                updated = _clone_device(current)
                updated.profile_mode = "global"
                updated.local_profile_payload = None
                updated.local_selected_sensors = None
                updated.local_sensor_preferences = None
                store.upsert(updated)
                trigger_reload()
                self._send_html(
                    _render_htmx_devices_table(filters),
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            toast_level="success",
                            toast_message=f"Restored global profile for {device_id}",
                        )
                    },
                )
                return True

            if parsed_path.path == "/htmx/devices/actions/metrics/clear":
                scope = (data.get("scope", [""])[0]).strip().lower()
                target_uid = (data.get("device_uid", [""])[0]).strip()
                if scope == "all":
                    target_uid = ""
                ok, message = _clear_metrics_scope(
                    device_uid=target_uid if target_uid else None
                )
                self._send_html(
                    _render_htmx_metrics_panel(),
                    status=HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            toast_level="success" if ok else "danger",
                            toast_message=message,
                        )
                    },
                )
                return True

            if parsed_path.path == "/htmx/logs/actions/clear":
                log_buffer.clear()
                LOG.debug("Cleared in-memory log buffer")
                self._send_html(
                    _render_htmx_logs_panel(),
                    status=HTTPStatus.OK,
                )
                return True

            if parsed_path.path == "/htmx/devices/actions/maintenance":
                action = data.get("action", [""])[0] if data.get("action") else ""
                ok, message, err = _execute_maintenance_action(action, data)
                toast_level = "success" if ok else "danger"
                toast_message = message if ok else (err or "Maintenance action failed")
                self._send_html(
                    _render_htmx_maintenance_panel(),
                    status=HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            toast_level=toast_level,
                            toast_message=toast_message,
                        )
                    },
                )
                return True

            if parsed_path.path == "/htmx/devices/actions/configuration":
                action = data.get("action", [""])[0] if data.get("action") else ""
                toast_level = "success"
                toast_message = ""
                status_code = HTTPStatus.OK
                if action == "set_log_level":
                    ok, message, err = _execute_maintenance_action(action, data)
                    toast_level = "success" if ok else "danger"
                    toast_message = (
                        message if ok else (err or "Configuration action failed")
                    )
                    status_code = HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST
                elif action == "set_theme":
                    selected_theme = _normalize_theme(
                        (data.get("theme", [""])[0]).strip()
                    )
                    theme_setter(selected_theme)
                    toast_message = f"Application theme set to {selected_theme}"
                elif action == "set_poll_timers":
                    metadata_raw = (
                        data.get("metadata_refresh_interval_seconds", [""])[0].strip()
                    )
                    idle_raw = (data.get("idle_reconnect_seconds", [""])[0]).strip()
                    try:
                        metadata_seconds = max(1, int(metadata_raw))
                    except ValueError:
                        metadata_seconds = -1
                    try:
                        idle_seconds = max(1.0, float(idle_raw))
                    except ValueError:
                        idle_seconds = -1.0
                    if metadata_seconds <= 0:
                        toast_level = "danger"
                        toast_message = (
                            "Metadata refresh interval must be a positive integer"
                        )
                        status_code = HTTPStatus.BAD_REQUEST
                    elif idle_seconds <= 0:
                        toast_level = "danger"
                        toast_message = (
                            "Idle reconnect interval must be a positive number"
                        )
                        status_code = HTTPStatus.BAD_REQUEST
                    else:
                        metadata_refresh_setter(metadata_seconds)
                        idle_reconnect_setter(idle_seconds)
                        toast_message = (
                            "Polling timers updated: "
                            f"metadata refresh={metadata_seconds}s, "
                            f"idle reconnect={idle_seconds:.1f}s"
                        )
                elif action == "set_ha_bridge_enabled":
                    raw_value = (data.get("ha_bridge_enabled", ["true"])[0]).strip()
                    enabled = raw_value.lower() in {"1", "true", "yes", "on"}
                    ha_bridge_enabled_setter(enabled)
                    toast_message = (
                        "Home Assistant bridge visibility "
                        f"{'enabled' if enabled else 'disabled'}"
                    )
                else:
                    selected = _normalize_timezone(
                        (data.get("timezone", [""])[0]).strip()
                    )
                    timezone_setter(selected)
                    toast_message = f"Application timezone set to {selected}"
                self._send_html(
                    _render_htmx_configuration_panel(),
                    status=status_code,
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            toast_level=toast_level,
                            toast_message=toast_message,
                        )
                    },
                )
                return True

            return False

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            parsed = parsed._replace(path=self._resolve_app_path(parsed.path))
            if self._handle_htmx_get(parsed):
                return
            if parsed.path == "/":
                location = self._prefixed_path("/htmx/devices")
                if parsed.query:
                    location += f"?{parsed.query}"
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", location)
                self.end_headers()
                return
            if parsed.path == "/favicon.ico" or parsed.path == "/favicon.png":
                if not FAVICON_PATH.exists():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                payload = FAVICON_PATH.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path == "/metrics.json":
                metrics_snapshot = get_metrics_snapshot()
                enriched_snapshot = _enrich_metrics_snapshot_with_identity(
                    metrics_snapshot=metrics_snapshot,
                    devices=store.list_devices(),
                )
                payload_json = json.dumps(
                    enriched_snapshot, indent=2, sort_keys=True
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload_json)))
                self.end_headers()
                self.wfile.write(payload_json)
                return
            if parsed.path == "/check-config.json":
                if not get_config:
                    result = {"status": "error", "message": "Config not available"}
                else:
                    config = get_config()
                    result = check_config(config, store.list_devices())
                payload_json = json.dumps(result, indent=2, sort_keys=True).encode(
                    "utf-8"
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload_json)))
                self.end_headers()
                self.wfile.write(payload_json)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            content_length = int(self.headers.get("Content-Length", "0"))
            content_type = self.headers.get("Content-Type", "")
            raw_body = self.rfile.read(content_length)

            parsed = urlparse(self.path)
            parsed = parsed._replace(path=self._resolve_app_path(parsed.path))

            # Handle multipart form data for file uploads
            if "multipart/form-data" in content_type:
                import email

                message = email.message_from_bytes(
                    b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw_body
                )
                data = {}
                payload = message.get_payload()
                parts = payload if isinstance(payload, list) else [message]
                for part in parts:
                    if part.get_content_disposition() == "form-data":
                        name = part.get_param("name", header="content-disposition")
                        if not name:
                            continue
                        part_bytes = part.get_payload(decode=True) or b""
                        value = _decode_http_text(part_bytes)
                        data[name] = [value]
            else:
                raw = _decode_http_text(raw_body)
                parsed_data = parse_qs(raw)
                data = parsed_data

            if self._handle_htmx_post(parsed, data):
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args) -> None:
            # Keep routine HTTP access traces available without polluting INFO logs.
            LOG.debug("%s - %s", self.client_address[0], fmt % args)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="ups2mqtt-web"
    )
    thread.start()
    LOG.info("Web UI listening on http://%s:%s", host, port)
    return server
