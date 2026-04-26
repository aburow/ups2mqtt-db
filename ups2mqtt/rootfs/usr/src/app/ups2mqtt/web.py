# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import html
import json
import logging
import secrets
import threading
from uuid import uuid4
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse
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
# Legacy cache reference for backward compatibility (now managed by catalog module)
APC_CATALOG_CACHE: dict[str, dict[str, list[dict[str, str]]]] = {}


class SessionStore:
    """Simple in-memory session store for flash messages and temporary data."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create_session(self) -> str:
        """Create a new session and return its ID."""
        session_id = secrets.token_hex(16)
        with self._lock:
            self._sessions[session_id] = {}
        return session_id

    def set_flash(self, session_id: str, msg: str = "", err: str = "") -> None:
        """Store flash message(s) in session."""
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = {}
            if msg:
                self._sessions[session_id]["msg"] = msg
            if err:
                self._sessions[session_id]["err"] = err

    def get_flash(self, session_id: str) -> tuple[str, str]:
        """Retrieve and clear flash messages from session."""
        with self._lock:
            if session_id not in self._sessions:
                return "", ""
            session = self._sessions[session_id]
            msg = session.pop("msg", "")
            err = session.pop("err", "")
            return msg, err

    def cleanup_session(self, session_id: str) -> None:
        """Remove a session."""
        with self._lock:
            self._sessions.pop(session_id, None)


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
        unit_id=unit_id,
        snmp_community=(data.get("snmp_community", ["public"])[0]).strip() or "public",
        poll_interval=poll_interval,
        name=(data.get("name", [""])[0]).strip() or None,
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
        unit_id=device.unit_id,
        snmp_community=device.snmp_community,
        poll_interval=device.poll_interval,
        name=device.name,
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
    lines = [
        "ID,Source,Host,Port,Unit,SNMP,Poll,Name,Debug,KeepConnectionOpen,Discovery,Polling"
    ]
    for d in devices:
        lines.append(
            f"{d.id},{d.source},{d.host},{d.port},{d.unit_id},{d.snmp_community},{d.poll_interval or ''},{(d.name or '').replace(',', ' ')},{d.debug_logging},{d.keep_connection_open},{d.discovery_enabled},{d.polling_enabled}"
        )
    return "\n".join(lines)


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
    capability_status: dict[str, str | int],
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
    get_capability_status: Callable[[], dict[str, str | int]],
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
) -> HTTPServer:
    session_store = SessionStore()
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
        "device_filter_source",
        "device_filter_host",
        "device_filter_port",
        "device_filter_unit",
        "device_filter_snmp",
        "device_filter_poll",
        "device_filter_name",
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

    def _device_matches_filters(device: DeviceConfig, filters: dict[str, str]) -> bool:
        if filters["device_filter_id"] and filters["device_filter_id"].lower() not in (
            device.id.lower()
        ):
            return False
        if filters["device_filter_source"] and filters[
            "device_filter_source"
        ].lower() not in (device.source.lower()):
            return False
        if filters["device_filter_host"] and filters[
            "device_filter_host"
        ].lower() not in (device.host.lower()):
            return False
        if filters["device_filter_port"] and filters[
            "device_filter_port"
        ].lower() not in (str(device.port).lower()):
            return False
        if filters["device_filter_unit"] and filters[
            "device_filter_unit"
        ].lower() not in (str(device.unit_id).lower()):
            return False
        if filters["device_filter_snmp"] and filters[
            "device_filter_snmp"
        ].lower() not in (device.snmp_community.lower()):
            return False
        poll_text = str(device.poll_interval or "").lower()
        if filters["device_filter_poll"] and filters[
            "device_filter_poll"
        ].lower() not in (poll_text):
            return False
        if filters["device_filter_name"] and filters[
            "device_filter_name"
        ].lower() not in ((device.name or "").lower()):
            return False
        return True

    def _filtered_sorted_devices(filters: dict[str, str]) -> list[DeviceConfig]:
        return sorted(
            (
                item
                for item in store.list_devices()
                if _device_matches_filters(item, filters)
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
        count = 0
        for row in lines[1:]:
            if len(row) < 12:
                LOG.warning("Skipping malformed CSV line: %s", row)
                continue
            try:
                dev = DeviceConfig(
                    id=str(row[0]).strip(),
                    source=str(row[1]).strip(),
                    host=str(row[2]).strip(),
                    port=int(row[3] or 502),
                    unit_id=int(row[4] or 1),
                    snmp_community=(str(row[5]).strip() or "public"),
                    poll_interval=int(row[6] or 0) if row[6] else None,
                    name=(str(row[7]).strip() or None),
                    debug_logging=str(row[8]).lower() in {"true", "1", "yes"},
                    keep_connection_open=str(row[9]).lower() in {"true", "1", "yes"},
                    discovery_enabled=str(row[10]).lower() in {"true", "1", "yes"},
                    polling_enabled=str(row[11]).lower() in {"true", "1", "yes"},
                )
                store.upsert(dev)
                count += 1
            except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                LOG.exception("CSV import error on row %r: %s", row, err)
        trigger_reload()
        return count

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
            "unit_id": "1",
            "snmp_community": "public",
            "poll_interval": "",
            "name": "",
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
            "unit_id": str(device.unit_id),
            "snmp_community": device.snmp_community,
            "poll_interval": (
                str(device.poll_interval) if device.poll_interval is not None else ""
            ),
            "name": device.name or "",
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
                "unit_id": (data.get("unit_id", [str(values["unit_id"])])[0]).strip(),
                "snmp_community": (
                    data.get("snmp_community", [str(values["snmp_community"])])[0]
                ).strip(),
                "poll_interval": (data.get("poll_interval", [""])[0]).strip(),
                "name": (data.get("name", [""])[0]).strip(),
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
            unit_id=unit_id,
            snmp_community=(data.get("snmp_community", ["public"])[0]).strip()
            or "public",
            poll_interval=poll_interval,
            name=(data.get("name", [""])[0]).strip() or None,
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
                )
                self._send_html(payload)
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

            if parsed_path.path == "/htmx/devices/actions/import_csv":
                csv_data = (data.get("csv_file", [""])[0]).strip()
                if not csv_data:
                    self._send_html(
                        _render_htmx_devices_table(filters),
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
                    _render_htmx_devices_table(filters),
                    headers={
                        "HX-Trigger": _hx_trigger_payload(
                            toast_level="success",
                            toast_message=f"Imported {imported} device(s) from CSV",
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

        def _redirect(
            self,
            msg: str = "",
            err: str = "",
            framed: bool = False,
            modal: str = "",
            modal_id: str = "",
        ) -> None:
            params: dict[str, str] = {}

            # Store flash messages in session instead of URL
            if msg or err:
                session_id = session_store.create_session()
                session_store.set_flash(session_id, msg, err)
                params["s"] = session_id

            if framed:
                params["framed"] = "1"
                # For framed mode, redirect to base URL with session
                # Do NOT include modal/id params - they should be cleared after form submission
            else:
                # For non-framed mode, preserve modal/id if provided
                if modal:
                    params["modal"] = modal
                if modal_id:
                    params["id"] = modal_id

            query = urlencode(params)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header(
                "Location", "/htmx/devices" + (f"?{query}" if query else "")
            )
            self.end_headers()

        def _render_tab_shell(self) -> str:
            return """<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <link rel="icon" type="image/png" href="/favicon.png"/>
  <title>ups2mqtt Device Manager</title>
  <style>
    :root {
      --bg: #ffffff;
      --fg: #1f2937;
      --muted-fg: #4b5563;
      --surface: #f9fafb;
      --border: #d1d5db;
      --tab-active-bg: #2563eb;
      --tab-border: #2563eb;
      --tab-inactive-fg: var(--muted-fg);
      --header-height: 56px;
    }
    [data-theme="dark"] {
      --bg: #111827;
      --fg: #e5e7eb;
      --muted-fg: #cbd5e1;
      --surface: #1f2937;
      --border: #374151;
    }
    * { box-sizing: border-box; }
    body { font-family: sans-serif; margin: 0; background: var(--bg); color: var(--fg); }
    .site-header {
      position: sticky; top: 0; z-index: 10;
      background: var(--surface); border-bottom: 1px solid var(--border);
      height: var(--header-height); display: flex; align-items: center; padding: 0 16px;
    }
    .site-title { font-size: 1rem; font-weight: 700; margin-right: 24px; white-space: nowrap; }
    .tab-nav { display: flex; gap: 2px; flex: 1; }
    .tab-btn {
      background: transparent; color: var(--tab-inactive-fg);
      border: none; border-bottom: 2px solid transparent;
      padding: 8px 16px; cursor: pointer; font-size: 0.9rem; font-weight: 500;
    }
    .tab-btn.active { color: var(--tab-active-bg); border-bottom-color: var(--tab-border); }
    #tab-frame {
      width: 100%;
      height: calc(100vh - var(--header-height));
      border: 0;
      display: block;
      background: var(--bg);
    }
  </style>
</head>
<body>
  <header class="site-header">
    <span class="site-title">ups2mqtt</span>
    <nav class="tab-nav">
      <button class="tab-btn" data-tab="devices">Devices</button>
      <button class="tab-btn" data-tab="metrics">Metrics</button>
      <button class="tab-btn" data-tab="logs">Logs</button>
      <button class="tab-btn" data-tab="maintenance">Maintenance</button>
    </nav>
    <span class="site-copyright">(c)aburow-2026</span>
  </header>
  <iframe id="tab-frame" title="ups2mqtt-tab-content"></iframe>
  <script>
    (() => {
      const TABS = ["devices", "metrics", "logs", "maintenance"];
      const DEFAULT_TAB = "devices";
      const LAST_TAB_KEY = "ups2mqtt_last_tab";
      const THEME_KEY = "ups2mqtt_theme";
      const root = document.documentElement;
      const frame = document.getElementById("tab-frame");

      const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      root.setAttribute("data-theme", localStorage.getItem(THEME_KEY) || preferred);

      function currentTab() {
        const hash = (location.hash || "").replace(/^#/, "");
        if (hash === "capabilities") {
          sessionStorage.setItem(LAST_TAB_KEY, "maintenance");
          return "maintenance";
        }
        if (hash && TABS.includes(hash)) {
          sessionStorage.setItem(LAST_TAB_KEY, hash);
          return hash;
        }
        return sessionStorage.getItem(LAST_TAB_KEY) || DEFAULT_TAB;
      }

      function frameSrc(tab) {
        const params = new URLSearchParams(window.location.search);
        params.delete("msg");
        params.delete("err");
        params.set("framed", "1");
        return `/?${params.toString()}#${tab}`;
      }

      function activate(tab) {
        const safeTab = TABS.includes(tab) ? tab : DEFAULT_TAB;
        document.querySelectorAll(".tab-btn[data-tab]").forEach((btn) => {
          btn.classList.toggle("active", btn.dataset.tab === safeTab);
        });
        frame.src = frameSrc(safeTab);
      }

      document.querySelectorAll(".tab-btn[data-tab]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const tab = btn.dataset.tab;
          if (location.hash !== `#${tab}`) {
            location.hash = tab;
          } else {
            activate(tab);
          }
        });
      });

      window.addEventListener("hashchange", () => activate(currentTab()));
      activate(currentTab());
    })();
  </script>
</body>
</html>"""

        def _render(self, custom_params: dict[str, list[str]] | None = None) -> str:
            if custom_params is None:
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
            else:
                params = custom_params
            framed_mode = params.get("framed", [""])[0] == "1"

            # Get flash messages from session (if present), otherwise from URL (legacy)
            session_id = params.get("s", [""])[0]
            if session_id:
                msg, err = session_store.get_flash(session_id)
                session_store.cleanup_session(session_id)
            else:
                msg = params.get("msg", [""])[0]
                err = params.get("err", [""])[0]
            modal_mode = params.get("modal", [""])[0]
            modal_device_id = params.get("id", [""])[0]
            device_sort = params.get("device_sort", ["id"])[0]
            device_dir = params.get("device_dir", ["asc"])[0]
            device_filter_id = params.get("device_filter_id", [""])[0].lower()
            device_filter_source = params.get("device_filter_source", [""])[0].lower()
            device_filter_host = params.get("device_filter_host", [""])[0].lower()
            device_filter_port = params.get("device_filter_port", [""])[0].lower()
            device_filter_unit = params.get("device_filter_unit", [""])[0].lower()
            device_filter_snmp = params.get("device_filter_snmp", [""])[0].lower()
            device_filter_poll = params.get("device_filter_poll", [""])[0].lower()
            device_filter_name = params.get("device_filter_name", [""])[0].lower()
            log_level = params.get("log_level", [""])[0]
            log_logger = params.get("log_logger", [""])[0]
            log_contains = params.get("log_contains", [""])[0]
            log_device = params.get("log_device", [""])[0]
            log_limit = _int_or_default(params.get("log_limit", ["150"])[0], 150)
            capability_status = get_capability_status()
            source_names = get_source_names()
            metrics = get_metrics_snapshot()
            current_runtime_log_level = logging.getLevelName(
                logging.getLogger().getEffectiveLevel()
            )
            maintenance = _prepare_maintenance_presentation(
                capability_status=capability_status,
                current_runtime_log_level=current_runtime_log_level,
            )

            valid_sort_fields = {
                "id",
                "source",
                "host",
                "port",
                "unit_id",
                "snmp_community",
                "poll_interval",
                "name",
                "debug_logging",
                "keep_connection_open",
                "discovery_enabled",
                "polling_enabled",
            }
            if device_sort not in valid_sort_fields:
                device_sort = "id"
            if device_dir not in {"asc", "desc"}:
                device_dir = "asc"

            def _parse_ip(ip_str: str) -> tuple:
                """Parse IP address into sortable tuple. Non-IPs sort last."""
                try:
                    parts = [int(p) for p in ip_str.split(".") if p]
                    if len(parts) == 4 and all(0 <= p <= 255 for p in parts):
                        return (0, tuple(parts))
                except (ValueError, AttributeError):
                    pass
                return (1, str(ip_str).lower())

            def _sort_value(device: DeviceConfig) -> tuple:
                value = getattr(device, device_sort, "")
                if value is None:
                    return (1, "")
                if isinstance(value, bool):
                    return (0, int(value))
                if isinstance(value, int):
                    return (0, value)
                if device_sort == "host":
                    return _parse_ip(str(value))
                return (0, str(value).lower())

            def _query_with(**updates: str) -> str:
                merged = {
                    key: values[-1]
                    for key, values in params.items()
                    if values and key not in {"modal", "id"}
                }
                for key, value in updates.items():
                    if value:
                        merged[key] = value
                    elif key in merged:
                        merged.pop(key)
                return "/?" + urlencode(merged)

            header_cells: list[str] = []
            sortable_headers = [
                ("ID", "id"),
                ("DRIVER", "source"),
                ("Host", "host"),
                ("Port", "port"),
                ("Unit", "unit_id"),
                ("SNMP Community", "snmp_community"),
                ("Poll", "poll_interval"),
                ("Name", "name"),
                ("Logging", "debug_logging"),
                ("Keep Conn", "keep_connection_open"),
                ("Discovery", "discovery_enabled"),
                ("Polling", "polling_enabled"),
            ]
            for label, field in sortable_headers:
                next_dir = (
                    "desc" if device_sort == field and device_dir == "asc" else "asc"
                )
                indicator = ""
                if device_sort == field:
                    indicator = " ↑" if device_dir == "asc" else " ↓"
                sort_link = _query_with(device_sort=field, device_dir=next_dir)
                header_cells.append(
                    f"<th><a href='{_escape(sort_link)}'>{_escape(label + indicator)}</a></th>"
                )
            header_cells.append("<th>Action</th>")
            device_header_html = "<tr>" + "".join(header_cells) + "</tr>"

            def _device_matches_filters(device: DeviceConfig) -> bool:
                """Check if device matches all active filters."""
                if device_filter_id and device_filter_id not in device.id.lower():
                    return False
                if (
                    device_filter_source
                    and device_filter_source not in device.source.lower()
                ):
                    return False
                if device_filter_host and device_filter_host not in device.host.lower():
                    return False
                if (
                    device_filter_port
                    and device_filter_port not in str(device.port).lower()
                ):
                    return False
                if (
                    device_filter_unit
                    and device_filter_unit not in str(device.unit_id).lower()
                ):
                    return False
                if (
                    device_filter_snmp
                    and device_filter_snmp not in device.snmp_community.lower()
                ):
                    return False
                if (
                    device_filter_poll
                    and device_filter_poll
                    not in str(device.poll_interval or "").lower()
                ):
                    return False
                if (
                    device_filter_name
                    and device_filter_name not in (device.name or "").lower()
                ):
                    return False
                return True

            rows: list[str] = []
            devices = sorted(
                (d for d in store.list_devices() if _device_matches_filters(d)),
                key=_sort_value,
                reverse=device_dir == "desc",
            )
            for device in devices:
                edit_link = "/?" + urlencode({"modal": "edit", "id": device.id})
                host_url = (
                    device.host
                    if device.host.startswith(("http://", "https://"))
                    else f"http://{device.host}"
                )
                rows.append(
                    "<tr>"
                    f"<td><a href='{_escape(edit_link)}'><button type='button'>{_escape(device.id)}</button></a></td>"
                    f"<td>{_escape(device.source)}</td>"
                    f"<td><a href='{_escape(host_url)}' target='_blank' rel='noopener noreferrer'>{_escape(device.host)}</a></td>"
                    f"<td>{device.port}</td>"
                    f"<td>{device.unit_id}</td>"
                    f"<td>{_escape(device.snmp_community)}</td>"
                    f"<td>{device.poll_interval or ''}</td>"
                    f"<td>{_escape(device.name or '')}</td>"
                    "<td>"
                    "<form method='post' style='display:inline'>"
                    "<input type='hidden' name='action' value='toggle_debug'/>"
                    f"<input type='hidden' name='id' value='{_escape(device.id)}'/>"
                    f"<button type='submit' class='badge {'badge-on' if device.debug_logging else 'badge-off'}'>{'On' if device.debug_logging else 'Off'}</button>"
                    "</form>"
                    "</td>"
                    "<td>"
                    "<form method='post' style='display:inline'>"
                    "<input type='hidden' name='action' value='toggle_keep_connection_open'/>"
                    f"<input type='hidden' name='id' value='{_escape(device.id)}'/>"
                    f"<button type='submit' class='badge {'badge-on' if device.keep_connection_open else 'badge-off'}'>{'On' if device.keep_connection_open else 'Off'}</button>"
                    "</form>"
                    "</td>"
                    "<td>"
                    "<form method='post' style='display:inline'>"
                    "<input type='hidden' name='action' value='toggle_discovery'/>"
                    f"<input type='hidden' name='id' value='{_escape(device.id)}'/>"
                    f"<button type='submit' class='badge {'badge-on' if device.discovery_enabled else 'badge-off'}'>{'On' if device.discovery_enabled else 'Off'}</button>"
                    "</form>"
                    "</td>"
                    "<td>"
                    "<form method='post' style='display:inline'>"
                    "<input type='hidden' name='action' value='toggle_polling'/>"
                    f"<input type='hidden' name='id' value='{_escape(device.id)}'/>"
                    f"<button type='submit' class='badge {'badge-on' if device.polling_enabled else 'badge-off'}'>{'On' if device.polling_enabled else 'Off'}</button>"
                    "</form>"
                    "</td>"
                    "<td>"
                    "<form method='post' style='display:inline'>"
                    "<input type='hidden' name='action' value='reinitialize'/>"
                    f"<input type='hidden' name='id' value='{_escape(device.id)}'/>"
                    "<button type='submit' class='btn' title='Clear and republish MQTT discovery'>Reinitialize</button>"
                    "</form>"
                    "</td>"
                    "<td>"
                    "<form method='post' style='display:inline'>"
                    "<input type='hidden' name='action' value='delete'/>"
                    f"<input type='hidden' name='id' value='{_escape(device.id)}'/>"
                    "<button type='submit' class='btn-danger'>Delete</button>"
                    "</form>"
                    "</td>"
                    "</tr>"
                )
            editing = modal_mode == "edit"
            form_device = store.get(modal_device_id) if editing else None
            if editing and form_device is None:
                err = err or f"Device {modal_device_id} not found"
                editing = False

            source_default = (
                form_device.source
                if form_device
                else (source_names[0] if source_names else "")
            )
            source_options = "".join(
                f"<option value='{_escape(name)}' {'selected' if name == source_default else ''}>{_escape(name)}</option>"
                for name in source_names
            )

            modal_html = ""
            if modal_mode in {"add", "edit"}:
                title = "Edit Device" if editing else "Add Device"
                form_id = form_device.id if form_device else ""
                form_host = form_device.host if form_device else ""
                form_port = str(form_device.port) if form_device else "502"
                form_unit = str(form_device.unit_id) if form_device else "1"
                form_comm = form_device.snmp_community if form_device else "public"
                form_poll = (
                    str(form_device.poll_interval)
                    if form_device and form_device.poll_interval is not None
                    else ""
                )
                form_name = form_device.name or "" if form_device else ""
                form_debug_checked = (
                    "checked" if form_device and form_device.debug_logging else ""
                )
                form_keep_connection_open_checked = (
                    "checked"
                    if form_device and form_device.keep_connection_open
                    else ""
                )
                form_discovery_checked = (
                    "checked"
                    if (form_device is None or form_device.discovery_enabled)
                    else ""
                )
                form_polling_checked = (
                    "checked"
                    if (form_device is None or form_device.polling_enabled)
                    else ""
                )
                original_id = form_device.id if form_device else ""
                form_uid = form_device.device_uid if form_device else ""

                modal_html = f"""
  <div class="modal-backdrop">
    <div class="modal">
      <h2>{_escape(title)}</h2>
      <form method="post">
        <input type="hidden" name="action" value="upsert"/>
        <input type="hidden" name="original_id" value="{_escape(original_id)}"/>
        <input type="hidden" name="device_uid" value="{_escape(form_uid)}"/>
        <div class="grid">
          <div><label>ID<input name="id" value="{_escape(form_id)}" required/></label></div>
          <div><label>Source<select name="source">{source_options}</select></label></div>
          <div><label>Host<input name="host" value="{_escape(form_host)}" required/></label></div>
          <div><label>Port<input name="port" value="{_escape(form_port)}"/></label></div>
          <div><label>Unit ID<input name="unit_id" value="{_escape(form_unit)}"/></label></div>
          <div><label>SNMP Community<input name="snmp_community" value="{_escape(form_comm)}"/></label></div>
          <div><label>Poll Interval (optional)<input name="poll_interval" value="{_escape(form_poll)}"/></label></div>
          <div><label>Name (optional)<input name="name" value="{_escape(form_name)}"/></label></div>
        </div>
        <p><label><input type="checkbox" name="debug_logging" {form_debug_checked}/> Enable verbose device logs</label></p>
        <p><label><input type="checkbox" name="keep_connection_open" {form_keep_connection_open_checked}/> Keep Modbus connection open</label></p>
        <p><label><input type="checkbox" name="discovery_enabled" {form_discovery_checked}/> Enable MQTT discovery</label></p>
        <p><label><input type="checkbox" name="polling_enabled" {form_polling_checked}/> Enable polling</label></p>
        <div class="modal-actions">
          <button type="submit" class="btn-primary">Save Device</button>
          <a href="/"><button type="button">Cancel</button></a>
        </div>
      </form>
    </div>
  </div>"""

            filtered_logs = log_buffer.query(
                level=log_level,
                logger=log_logger,
                contains=log_contains,
                device=log_device,
                limit=log_limit,
            )
            current_timezone = _normalize_timezone(timezone_getter())
            prepared_logs = _prepare_logs_presentation(
                filtered_logs,
                timezone_name=current_timezone,
            )
            log_rows = []
            for entry in prepared_logs:
                log_rows.append(
                    "<tr>"
                    f"<td>{_escape(entry['ts'])}</td>"
                    f"<td><span class='log-badge {_escape(entry['level_class'])}'>{_escape(entry['level'])}</span></td>"
                    f"<td>{_escape(entry['logger'])}</td>"
                    f"<td>{_escape(entry['device'])}</td>"
                    f"<td>{_escape(entry['message'])}</td>"
                    "</tr>"
                )

            prepared_metrics = _prepare_metrics_presentation(
                metrics=metrics,
                devices=store.list_devices(),
                timezone_name=current_timezone,
            )
            metrics_generated_at_utc = str(prepared_metrics["generated_at_utc"])
            metrics_timezone_label = str(prepared_metrics["timezone_label"])
            metrics_totals = prepared_metrics["totals"]
            total_failed_timeout = int(prepared_metrics["total_failed_timeout"])
            metrics_backpressure = prepared_metrics["backpressure"]
            metric_rows: list[str] = []
            for item in prepared_metrics["rows"]:
                metric_rows.append(
                    "<tr>"
                    f"<td>{_escape(str(item.get('name_with_uid', item.get('name', ''))))}</td>"
                    f"<td>{_escape(str(item.get('status', 'unknown')))}</td>"
                    f"<td>{int(item.get('started', 0))}</td>"
                    f"<td>{int(item.get('success', 0))}</td>"
                    f"<td>{int(item.get('failed', 0))}</td>"
                    f"<td>{int(item.get('timeout', 0))}</td>"
                    f"<td>{_escape(str(item.get('min_ms', '')))}</td>"
                    f"<td>{_escape(str(item.get('avg_ms', '')))}</td>"
                    f"<td>{_escape(str(item.get('max_ms', '')))}</td>"
                    f"<td>{_escape(str(item.get('last_ms', '')))}</td>"
                    f"<td>{int(item.get('values', 0))}</td>"
                    f"<td>{_escape(str(item.get('last_error', '')))}</td>"
                    f"<td>{_escape(str(item.get('updated_utc', '')))}</td>"
                    "</tr>"
                )

            flash_html = ""
            if msg:
                flash_html += f"<div class='flash flash-msg' id='flash-msg'><span>{_escape(msg)}</span><button class='flash-close' onclick='this.parentElement.remove()'>&#x2715;</button></div>"
            if err:
                flash_html += f"<div class='flash flash-err' id='flash-err'><span>{_escape(err)}</span><button class='flash-close' onclick='this.parentElement.remove()'>&#x2715;</button></div>"

            return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  {"<base target='_top'/>" if framed_mode else ""}
  <link rel="icon" type="image/png" href="/favicon.png"/>
  <title>ups2mqtt Device Manager</title>
  <style>
    :root {{
      --bg: #ffffff;
      --fg: #1f2937;
      --muted-fg: #4b5563;
      --surface: #f9fafb;
      --surface-2: #f3f4f6;
      --border: #d1d5db;
      --msg-bg: #eaf7ea;
      --msg-border: #9ad49a;
      --err-bg: #fce8e8;
      --err-border: #e5a7a7;
      --btn-bg: #e5e7eb;
      --btn-fg: #111827;
      --btn-border: #9ca3af;
      --code-bg: #f3f4f6;
      --tab-active-bg: #2563eb;
      --tab-active-fg: #ffffff;
      --tab-border: #2563eb;
      --tab-inactive-fg: var(--muted-fg);
      --btn-primary-bg: #2563eb;
      --btn-primary-fg: #ffffff;
      --btn-primary-border: #1d4ed8;
      --btn-danger-bg: #dc2626;
      --btn-danger-fg: #ffffff;
      --btn-danger-border: #b91c1c;
      --badge-on-bg: #dcfce7;
      --badge-on-fg: #166534;
      --badge-off-bg: #fee2e2;
      --badge-off-fg: #991b1b;
      --log-error-bg: #fee2e2;
      --log-error-fg: #991b1b;
      --log-warning-bg: #fef3c7;
      --log-warning-fg: #92400e;
      --log-info-bg: #dbeafe;
      --log-info-fg: #1e40af;
      --log-debug-bg: #f3f4f6;
      --log-debug-fg: #6b7280;
      --card-bg: var(--surface);
      --card-border: var(--border);
      --card-shadow: 0 1px 3px rgba(0,0,0,0.08);
      --header-bg: var(--surface);
      --header-border: var(--border);
      --header-height: 56px;
    }}
    [data-theme="dark"] {{
      --bg: #111827;
      --fg: #e5e7eb;
      --muted-fg: #cbd5e1;
      --surface: #1f2937;
      --surface-2: #111827;
      --border: #374151;
      --msg-bg: #12311a;
      --msg-border: #2d7d46;
      --err-bg: #3d1a1a;
      --err-border: #8f3c3c;
      --btn-bg: #374151;
      --btn-fg: #f3f4f6;
      --btn-border: #4b5563;
      --code-bg: #1f2937;
      --badge-on-bg: #14532d;
      --badge-on-fg: #86efac;
      --badge-off-bg: #450a0a;
      --badge-off-fg: #fca5a5;
      --log-error-bg: #450a0a;
      --log-error-fg: #fca5a5;
      --log-warning-bg: #451a03;
      --log-warning-fg: #fcd34d;
      --log-info-bg: #1e3a5f;
      --log-info-fg: #93c5fd;
      --log-debug-bg: #1f2937;
      --log-debug-fg: #9ca3af;
      --card-shadow: 0 1px 3px rgba(0,0,0,0.4);
    }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: sans-serif; margin: 0; background: var(--bg); color: var(--fg); }}
    .site-header {{
      position: sticky; top: 0; z-index: 10;
      background: var(--header-bg); border-bottom: 1px solid var(--header-border);
      height: var(--header-height); display: flex; align-items: center; padding: 0 16px;
    }}
    .site-title {{ font-size: 1rem; font-weight: 700; margin-right: 24px; white-space: nowrap; }}
    .tab-nav {{ display: flex; gap: 2px; flex: 1; }}
    .site-copyright {{ font-size: 0.75rem; color: var(--muted-fg); white-space: nowrap; margin-left: auto; }}
    .tab-btn {{
      background: transparent; color: var(--tab-inactive-fg);
      border: none; border-bottom: 2px solid transparent;
      padding: 8px 16px; cursor: pointer; font-size: 0.9rem; font-weight: 500;
      transition: color 0.15s, border-color 0.15s;
    }}
    .tab-btn.active {{
      color: var(--tab-active-bg); border-bottom-color: var(--tab-border);
    }}
    .header-right {{ display: flex; align-items: center; gap: 8px; }}
    .page-wrap {{ max-width: 1280px; margin: 0 auto; padding: 0 16px 40px; }}
    .tab-section {{ display: none; padding-top: 24px; }}
    .tab-section.visible {{ display: block; }}
    .flash {{
      padding: 10px 14px; border-radius: 6px; margin-bottom: 16px;
      display: flex; justify-content: space-between; align-items: center;
    }}
    .flash-msg {{ background: var(--msg-bg); border: 1px solid var(--msg-border); color: var(--fg); }}
    .flash-err {{ background: var(--err-bg); border: 1px solid var(--err-border); color: var(--fg); }}
    .flash-close {{ background: none; border: none; cursor: pointer; font-size: 1.1rem;
                     color: var(--muted-fg); padding: 0 4px; }}
    .stat-cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }}
    .stat-card {{
      background: var(--card-bg); border: 1px solid var(--card-border);
      border-radius: 8px; padding: 16px 20px; box-shadow: var(--card-shadow);
    }}
    .stat-label {{ font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
                    letter-spacing: 0.05em; color: var(--muted-fg); margin-bottom: 4px; }}
    .stat-value {{ font-size: 1.75rem; font-weight: 700; }}
    button, .btn {{ background: var(--btn-bg); color: var(--btn-fg); border: 1px solid var(--btn-border);
                    padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 0.875rem;
                    font-weight: 500; }}
    .btn-primary {{ background: var(--btn-primary-bg); color: var(--btn-primary-fg);
                    border-color: var(--btn-primary-border); }}
    .btn-danger {{ background: var(--btn-danger-bg); color: var(--btn-danger-fg);
                   border-color: var(--btn-danger-border); }}
    .badge {{
      display: inline-block; padding: 2px 10px; border-radius: 999px;
      font-size: 0.75rem; font-weight: 600; cursor: pointer; border: none;
      white-space: nowrap;
    }}
    .badge-on {{ background: var(--badge-on-bg); color: var(--badge-on-fg); }}
    .badge-off {{ background: var(--badge-off-bg); color: var(--badge-off-fg); }}
    .log-badge {{
      display: inline-block; padding: 1px 7px; border-radius: 4px;
      font-size: 0.7rem; font-weight: 700; font-family: monospace;
    }}
    .log-ERROR {{ background: var(--log-error-bg); color: var(--log-error-fg); }}
    .log-WARNING {{ background: var(--log-warning-bg); color: var(--log-warning-fg); }}
    .log-INFO {{ background: var(--log-info-bg); color: var(--log-info-fg); }}
    .log-DEBUG {{ background: var(--log-debug-bg); color: var(--log-debug-fg); }}
    .cap-card {{
      background: var(--card-bg); border: 1px solid var(--card-border);
      border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: var(--card-shadow);
    }}
    .cap-card h3 {{ margin: 0 0 12px; font-size: 1rem; }}
    .cap-meta-row {{
      display: flex; flex-wrap: wrap; gap: 16px; font-size: 0.85rem;
      color: var(--muted-fg); margin-bottom: 16px;
    }}
    .cap-meta-row strong {{ color: var(--fg); }}
    .cap-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .section-header {{
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 16px;
    }}
    .section-header h2 {{ margin: 0; font-size: 1.125rem; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 0.875rem; }}
    th, td {{ border: 1px solid var(--border); padding: 8px 10px; text-align: left; }}
    th {{ background: var(--surface-2); font-weight: 600; font-size: 0.8rem;
         text-transform: uppercase; letter-spacing: 0.03em; }}
    th a {{ color: var(--fg); text-decoration: none; }}
    .logs td {{ font-family: monospace; font-size: 11px; vertical-align: top; }}
    .logs-filters {{ display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr));
                     gap: 8px; margin: 12px 0 16px; }}
    .device-filters {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
                       gap: 8px; margin: 12px 0 16px; }}
    input, select {{ padding: 6px 8px; margin: 2px 0; width: 100%; box-sizing: border-box;
                     background: var(--surface); color: var(--fg); border: 1px solid var(--border);
                     border-radius: 4px; font-size: 0.875rem; }}
    a {{ color: var(--fg); }}
    code {{ background: var(--code-bg); padding: 2px 4px; border-radius: 4px; }}
    .modal-backdrop {{
      position: fixed; inset: 0; background: rgba(0,0,0,0.45);
      display: flex; align-items: center; justify-content: center; z-index: 20;
    }}
    .modal {{
      background: var(--surface); border-radius: 10px; width: min(900px, 95vw);
      padding: 24px; color: var(--fg); border: 1px solid var(--border);
      box-shadow: 0 16px 48px rgba(0,0,0,0.28);
    }}
    .modal h2 {{ margin: 0 0 20px; font-size: 1.125rem; }}
    .modal .grid {{
      display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 10px; margin-bottom: 16px;
    }}
    .modal label {{ display: block; font-size: 0.8rem; font-weight: 500; color: var(--muted-fg);
                    margin-bottom: 4px; }}
    .modal-actions {{ display: flex; gap: 8px; margin-top: 16px; }}
    {
                " .site-header { display: none; } .page-wrap { max-width: 100%; padding-top: 16px; } "
                if framed_mode
                else ""
            }
  </style>
</head>
<body>
  <header class="site-header">
    <span class="site-title">ups2mqtt</span>
    <nav class="tab-nav">
      <button class="tab-btn" data-tab="devices">Devices</button>
      <button class="tab-btn" data-tab="metrics">Metrics</button>
      <button class="tab-btn" data-tab="logs">Logs</button>
      <button class="tab-btn" data-tab="maintenance">Maintenance</button>
    </nav>
    <div class="header-right">
      <button type="button" id="check-config-btn" title="Check configuration and connectivity">Check Config</button>
      <button type="button" id="theme-toggle">Switch to Dark</button>
    </div>
  </header>
  <div class="page-wrap">
    {flash_html}
    <section class="tab-section" id="tab-devices">
      <div class="section-header">
        <h2>Devices</h2>
        <div style="display: flex; gap: 8px; margin-left: auto;">
          <a href="/?modal=add"><button type="button" class="btn-primary">Add Device</button></a>
          <button type="button" class="btn-primary" onclick="document.getElementById('exportModal').style.display='flex'">Export</button>
          <button type="button" class="btn-primary" onclick="document.getElementById('importModal').style.display='flex'">Import</button>
        </div>
      </div>
      <!-- Export Modal -->
      <div id="exportModal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.45); align-items: center; justify-content: center; z-index: 30;">
        <div style="background: var(--surface); border-radius: 10px; width: min(600px, 95vw); padding: 24px; color: var(--fg); border: 1px solid var(--border); box-shadow: 0 16px 48px rgba(0,0,0,0.28);">
          <h2 style="margin: 0 0 20px;">Export Devices</h2>
          <p style="margin: 0 0 20px;">Download all devices as a CSV file.</p>
          <div style="display: flex; gap: 8px; margin-top: 16px;">
            <form method="get" action="/export-csv" style="margin: 0;">
              <button type="submit" class="btn-primary" style="padding: 8px 16px; background: var(--tab-active-bg); color: white; border: none; border-radius: 4px; cursor: pointer;">Download CSV</button>
            </form>
            <button onclick="document.getElementById('exportModal').style.display='none'" style="padding: 8px 16px; background: var(--border); color: var(--fg); border: none; border-radius: 4px; cursor: pointer;">Cancel</button>
          </div>
        </div>
      </div>
      <!-- Import Modal -->
      <div id="importModal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.45); align-items: center; justify-content: center; z-index: 30;">
        <div style="background: var(--surface); border-radius: 10px; width: min(600px, 95vw); padding: 24px; color: var(--fg); border: 1px solid var(--border); box-shadow: 0 16px 48px rgba(0,0,0,0.28);">
          <h2 style="margin: 0 0 20px;">Import Devices</h2>
          <p style="margin: 0 0 20px;">Upload a CSV file to import devices. Expected format: ID,Source,Host,Port,Unit,SNMP,Poll,Name,Debug,Discovery,Polling</p>
          <form method="post" action="/?framed=1" enctype="multipart/form-data" style="margin: 0;">
            <input type="file" name="csv_file" accept=".csv" style="width: 100%; padding: 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--fg); margin-bottom: 16px;" required>
            <input type="hidden" name="action" value="import_csv">
            <div style="display: flex; gap: 8px; margin-top: 16px;">
              <button type="submit" class="btn-primary" style="padding: 8px 16px; background: var(--tab-active-bg); color: white; border: none; border-radius: 4px; cursor: pointer;">Import</button>
              <button type="button" onclick="document.getElementById('importModal').style.display='none'" style="padding: 8px 16px; background: var(--border); color: var(--fg); border: none; border-radius: 4px; cursor: pointer;">Cancel</button>
            </div>
          </form>
        </div>
      </div>
      <form method="get">
        <div class="device-filters">
          <div><label>ID<input name="device_filter_id" value="{
                _escape(device_filter_id)
            }" placeholder="garage"/></label></div>
          <div><label>Source<input name="device_filter_source" value="{
                _escape(device_filter_source)
            }" placeholder="snmp"/></label></div>
          <div><label>Host<input name="device_filter_host" value="{
                _escape(device_filter_host)
            }" placeholder="192.168"/></label></div>
          <div><label>Port<input name="device_filter_port" value="{
                _escape(device_filter_port)
            }" placeholder="161"/></label></div>
          <div><label>Unit<input name="device_filter_unit" value="{
                _escape(device_filter_unit)
            }" placeholder="1"/></label></div>
          <div><label>SNMP Community<input name="device_filter_snmp" value="{
                _escape(device_filter_snmp)
            }" placeholder="public"/></label></div>
          <div><label>Poll<input name="device_filter_poll" value="{
                _escape(device_filter_poll)
            }" placeholder="30"/></label></div>
          <div><label>Name<input name="device_filter_name" value="{
                _escape(device_filter_name)
            }" placeholder="UPS"/></label></div>
        </div>
        <p>
          <button type="submit" class="btn-primary">Apply Filters</button>
          <a href="/#devices"><button type="button">Clear</button></a>
        </p>
      </form>
      <table>
        <thead>
          {device_header_html}
        </thead>
        <tbody>
          {
                "".join(rows)
                if rows
                else "<tr><td colspan='12'>No devices configured.</td></tr>"
            }
        </tbody>
      </table>
    </section>
    <section class="tab-section" id="tab-metrics">
      <div class="section-header">
        <h2>Metrics</h2>
        <div style="display:flex;gap:12px;align-items:center;">
          <span style="font-size:0.8rem;color:var(--muted-fg)">
            Updated {_escape(metrics_timezone_label)}: {
                _escape(metrics_generated_at_utc)
            }
            &nbsp;|&nbsp; <a href="/metrics.json">/metrics.json</a>
          </span>
          <form method="post" style="margin:0;">
            <input type="hidden" name="action" value="clear_metrics"/>
            <button type="submit" class="btn-secondary" style="font-size:0.8rem;padding:4px 12px;">Clear Metrics</button>
          </form>
        </div>
      </div>
      <div class="stat-cards">
        <div class="stat-card">
          <div class="stat-label">Devices</div>
          <div class="stat-value">{_escape(str(metrics_totals.get("devices", 0)))}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Polls Started</div>
          <div class="stat-value">{
                _escape(str(metrics_totals.get("polls_started", 0)))
            }</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Succeeded</div>
          <div class="stat-value" style="color:var(--badge-on-fg)">{
                _escape(str(metrics_totals.get("polls_succeeded", 0)))
            }</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Failed / Timeout</div>
          <div class="stat-value" style="color:var(--badge-off-fg)">{
                total_failed_timeout
            }</div>
        </div>
      </div>
      <div style="margin-bottom: 16px; padding: 12px; background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 8px;">
        <div style="font-size: 0.85rem; color: var(--muted-fg); margin-bottom: 8px;"><strong>Backpressure:</strong></div>
        <div style="display: flex; gap: 16px; font-size: 0.9rem;">
          <span>Polls in-flight: <strong>{
                metrics_backpressure.get("polls_in_flight", 0)
            }</strong></span>
          <span>Semaphore available: <strong>{
                metrics_backpressure.get("semaphore_available", 0)
            }</strong> / {capability_status.get("max_concurrent_polls", "?")}</span>
        </div>
      </div>
      <table>
        <thead><tr><th>Device</th><th>Status</th><th>Started</th><th>Success</th><th>Failed</th><th>Timeout</th><th>Min ms</th><th>Avg ms</th><th>Max ms</th><th>Last ms</th><th>Values</th><th>Last Error</th><th>Updated {
                _escape(metrics_timezone_label)
            }</th></tr></thead>
        <tbody>
          {
                "".join(metric_rows)
                if metric_rows
                else "<tr><td colspan='13'>No metrics yet.</td></tr>"
            }
        </tbody>
      </table>
    </section>
    <section class="tab-section" id="tab-logs">
      <div class="section-header"><h2>Logs</h2></div>
      <form method="get">
        <div class="logs-filters">
          <div>
            <label>Level
              <select name="log_level">
                <option value="" {"selected" if not log_level else ""}>Any</option>
                <option value="DEBUG" {
                "selected" if log_level == "DEBUG" else ""
            }>DEBUG</option>
                <option value="INFO" {
                "selected" if log_level == "INFO" else ""
            }>INFO</option>
                <option value="WARNING" {
                "selected" if log_level == "WARNING" else ""
            }>WARNING</option>
                <option value="ERROR" {
                "selected" if log_level == "ERROR" else ""
            }>ERROR</option>
              </select>
            </label>
          </div>
          <div><label>Device<input name="log_device" value="{
                _escape(log_device)
            }" placeholder="GarageUPS"/></label></div>
          <div><label>Logger<input name="log_logger" value="{
                _escape(log_logger)
            }" placeholder="ups2mqtt"/></label></div>
          <div><label>Contains<input name="log_contains" value="{
                _escape(log_contains)
            }" placeholder="Polling"/></label></div>
          <div><label>Limit<input name="log_limit" value="{log_limit}"/></label></div>
        </div>
        <p>
          <button type="submit" class="btn-primary">Apply Filters</button>
          <a href="/"><button type="button">Clear</button></a>
        </p>
      </form>
      <table class="logs">
        <thead><tr><th>{
                _escape(current_timezone)
            } Time</th><th>Level</th><th>Logger</th><th>Device</th><th>Message</th></tr></thead>
        <tbody>
          {
                "".join(log_rows)
                if log_rows
                else "<tr><td colspan='5'>No log entries for current filter.</td></tr>"
            }
        </tbody>
      </table>
    </section>
    <section class="tab-section" id="tab-maintenance">
      <div class="section-header"><h2>Maintenance</h2></div>
      <div class="cap-card">
        <h3>System Info</h3>
        <div class="cap-meta-row">
          <span>Source: <strong>{_escape(str(maintenance["source"]))}</strong></span>
          <span>Profiles: <strong>{
                _escape(str(maintenance["profile_count"]))
            }</strong></span>
          <span>Apps dir: <code>{_escape(str(maintenance["apps_dir"]))}</code></span>
          <span>Max concurrent polls: <strong>{
                _escape(str(maintenance["max_concurrent_polls"]))
            }</strong></span>
          <span>Runtime log level: <strong>{
                _escape(str(maintenance["runtime_log_level"]))
            }</strong></span>
        </div>
        <div class="cap-actions">
          <form method="post" style="display:inline">
            <input type="hidden" name="action" value="republish_discovery"/>
            <button type="submit">Republish MQTT Discovery</button>
          </form>
          <form method="post" style="display:inline">
            <input type="hidden" name="action" value="cleanup_db"/>
            <button type="submit">Cleanup SQLite State</button>
          </form>
        </div>
        <div class="cap-actions" style="margin-top: 10px;">
          <form method="post" style="display:inline-flex; gap: 8px; align-items: center;">
            <input type="hidden" name="action" value="set_log_level"/>
            <label style="font-size: 0.9rem;">Set Runtime Log Level</label>
            <select name="runtime_log_level">
              {
                "".join(
                    f"<option value='{_escape(level)}' {'selected' if str(maintenance['runtime_log_level']) == level else ''}>{_escape(level)}</option>"
                    for level in maintenance["runtime_log_levels"]
                )
            }
            </select>
            <button type="submit">Apply</button>
          </form>
        </div>
      </div>
    </section>
  </div>
  {modal_html}
  <script>
    (() => {{
      // Detect if framed content is loaded in top window (not in iframe)
      // This happens when redirects navigate the main browser instead of the iframe
      const params = new URLSearchParams(window.location.search);
      const isFramed = params.get("framed") === "1";
      const inIframe = window.self !== window.top;

      if (isFramed && !inIframe) {{
        // We have framed=1 but we're NOT in an iframe - redirect to root
        window.location.href = "/";
        return;
      }}

      // Theme management
      const storageKey = "ups2mqtt_theme";
      const root = document.documentElement;
      const toggle = document.getElementById("theme-toggle");
      const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      const initial = localStorage.getItem(storageKey) || preferred;
      const applyTheme = (theme) => {{
        root.setAttribute("data-theme", theme);
        if (toggle) {{
          toggle.textContent = theme === "dark" ? "Switch to Light" : "Switch to Dark";
        }}
      }};
      applyTheme(initial);
      if (toggle) {{
        toggle.addEventListener("click", () => {{
          const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
          localStorage.setItem(storageKey, next);
          applyTheme(next);
        }});
      }}

      // Tab switching with hash-based navigation
      const TABS = ["devices", "metrics", "logs", "maintenance"];
      const DEFAULT_TAB = "devices";
      const LAST_TAB_KEY = "ups2mqtt_last_tab";

      function tabFromHash() {{
        const hash = (location.hash || "").replace(/^#/, "");
        if (hash === "capabilities") {{
          sessionStorage.setItem(LAST_TAB_KEY, "maintenance");
          return "maintenance";
        }}
        if (hash && TABS.includes(hash)) {{
          sessionStorage.setItem(LAST_TAB_KEY, hash);
          return hash;
        }}
        return sessionStorage.getItem(LAST_TAB_KEY) || DEFAULT_TAB;
      }}

      function activateTab(name) {{
        const tab = TABS.includes(name) ? name : DEFAULT_TAB;
        TABS.forEach(t => {{
          const section = document.getElementById("tab-" + t);
          const btn = document.querySelector(`.tab-btn[data-tab="${{t}}"]`);
          if (section) section.classList.toggle("visible", t === tab);
          if (btn) btn.classList.toggle("active", t === tab);
        }});
      }}

      activateTab(tabFromHash());
      document.querySelectorAll(".tab-btn[data-tab]").forEach(btn => {{
        btn.addEventListener("click", () => {{
          location.hash = btn.dataset.tab;
        }});
      }});
      window.addEventListener("hashchange", () => activateTab(tabFromHash()));

      // Flash auto-dismiss and URL cleanup
      ["flash-msg", "flash-err"].forEach(id => {{
        const el = document.getElementById(id);
        if (el) {{
          // Clean session/msg/err/modal params from URL to prevent replay on reload
          const url = new URL(window.location);
          url.searchParams.delete("s");
          url.searchParams.delete("msg");
          url.searchParams.delete("err");
          url.searchParams.delete("modal");
          url.searchParams.delete("id");
          window.history.replaceState({{}}, "", url.toString());
          // Remove banner after 5s
          setTimeout(() => el.remove(), 5000);
        }}
      }});

      // Check Config button handler
      const checkConfigBtn = document.getElementById("check-config-btn");
      if (checkConfigBtn) {{
        checkConfigBtn.addEventListener("click", async () => {{
          const originalText = checkConfigBtn.textContent;
          checkConfigBtn.textContent = "Checking...";
          checkConfigBtn.disabled = true;

          try {{
            const response = await fetch("/check-config.json");
            const result = await response.json();

            let msg = result.summary || "Check complete";
            if (result.status === "ok") {{
              let details = [];
              if (result.mqtt) {{
                details.push(`MQTT: ${{result.mqtt.status}} (${{result.mqtt.host}}:${{result.mqtt.port}})`);
              }}
              if (result.ha_api && result.ha_api.status !== "skipped") {{
                details.push(`HA API: ${{result.ha_api.status}}`);
              }}
              const failedDevices = Object.entries(result.devices || {{}}).filter(([_, cfg]) => cfg.status !== "ok").map(([id]) => id);
              if (failedDevices.length > 0) {{
                details.push(`Devices: ${{failedDevices.length}} issues`);
              }} else {{
                details.push("All devices OK");
              }}
              msg += " - " + details.join(" | ");
            }}

            const url = new URL(window.location);
            url.searchParams.set("msg", msg);
            window.location = url.toString();
          }} catch (err) {{
            const url = new URL(window.location);
            url.searchParams.set("err", "Config check failed: " + err.message);
            window.location = url.toString();
          }} finally {{
            checkConfigBtn.textContent = originalText;
            checkConfigBtn.disabled = false;
          }}
        }});
      }}
    }})();
  </script>
</body>
</html>"""

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if self._handle_htmx_get(parsed):
                return
            if parsed.path == "/":
                location = "/htmx/devices"
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
            if parsed.path == "/export-csv":
                devices = store.list_devices()
                csv_data = _generate_devices_csv(devices)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition", "attachment; filename=devices.csv"
                )
                self.end_headers()
                self.wfile.write(csv_data.encode("utf-8"))
                return
            params = parse_qs(parsed.query)
            framed_mode = params.get("framed", [""])[0] == "1"
            payload = (
                self._render().encode("utf-8")
                if framed_mode
                else self._render_tab_shell().encode("utf-8")
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802
            content_length = int(self.headers.get("Content-Length", "0"))
            content_type = self.headers.get("Content-Type", "")
            raw_body = self.rfile.read(content_length)

            # Extract framed param from request path
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            is_framed = params.get("framed", [""])[0] == "1"

            # Handle multipart form data for file uploads
            if "multipart/form-data" in content_type:
                import email

                message = email.message_from_bytes(
                    b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw_body
                )
                data = {}
                for part in message.get_payload():
                    if part.get_content_disposition() == "form-data":
                        name = part.get_param("name", header="content-disposition")
                        if not name:
                            continue
                        value = part.get_payload(decode=True).decode("utf-8")
                        if name == "csv_file":
                            data["csv_file"] = [value]
                        else:
                            data[name] = [value]
            else:
                raw = raw_body.decode("utf-8")
                parsed_data = parse_qs(raw)
                data = parsed_data

            action = data.get("action", [""])[0] if data.get("action") else ""

            if self._handle_htmx_post(parsed, data):
                return

            try:
                if action == "import_csv":
                    csv_data = (data.get("csv_file", [""])[0]).strip()
                    if not csv_data:
                        self._redirect(msg="No CSV file provided", framed=is_framed)
                        return
                    count = _import_devices_from_csv_data(csv_data)
                    self._redirect(
                        msg=f"Imported {count} device(s) from CSV.", framed=is_framed
                    )
                    return
                if action == "upsert":
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
                    self._redirect(msg=f"Saved device {device.id}", framed=is_framed)
                    return
                if action in {
                    "init_capabilities",
                    "republish_discovery",
                    "cleanup_db",
                    "set_log_level",
                }:
                    ok, message, err = _execute_maintenance_action(action, data)
                    self._redirect(
                        msg=message if ok else "",
                        err=err if not ok else "",
                        framed=is_framed,
                    )
                    return
                if action == "toggle_debug":
                    device_id = (data.get("id", [""])[0]).strip()
                    if not device_id:
                        raise ValueError("Missing device id")
                    current = store.get(device_id)
                    if current is None:
                        raise ValueError(f"Device {device_id} not found")
                    updated = _clone_device(
                        current,
                        debug_logging=not current.debug_logging,
                    )
                    store.upsert(updated)
                    trigger_reload()
                    status = "enabled" if updated.debug_logging else "disabled"
                    self._redirect(
                        msg=f"Debug logging {status} for {device_id}", framed=is_framed
                    )
                    return
                if action == "toggle_discovery":
                    device_id = (data.get("id", [""])[0]).strip()
                    if not device_id:
                        raise ValueError("Missing device id")
                    current = store.get(device_id)
                    if current is None:
                        raise ValueError(f"Device {device_id} not found")
                    updated = _clone_device(
                        current,
                        discovery_enabled=not current.discovery_enabled,
                    )
                    store.upsert(updated)
                    trigger_reload()
                    status = "enabled" if updated.discovery_enabled else "disabled"
                    self._redirect(
                        msg=f"Discovery {status} for {device_id}", framed=is_framed
                    )
                    return
                if action == "toggle_keep_connection_open":
                    device_id = (data.get("id", [""])[0]).strip()
                    if not device_id:
                        raise ValueError("Missing device id")
                    current = store.get(device_id)
                    if current is None:
                        raise ValueError(f"Device {device_id} not found")
                    updated = _clone_device(
                        current,
                        keep_connection_open=not current.keep_connection_open,
                    )
                    store.upsert(updated)
                    trigger_reload()
                    status = "enabled" if updated.keep_connection_open else "disabled"
                    self._redirect(
                        msg=f"Keep-connection-open {status} for {device_id}",
                        framed=is_framed,
                    )
                    return
                if action == "toggle_polling":
                    device_id = (data.get("id", [""])[0]).strip()
                    if not device_id:
                        raise ValueError("Missing device id")
                    current = store.get(device_id)
                    if current is None:
                        raise ValueError(f"Device {device_id} not found")
                    updated = _clone_device(
                        current,
                        polling_enabled=not current.polling_enabled,
                    )
                    store.upsert(updated)
                    trigger_reload()
                    status = "enabled" if updated.polling_enabled else "disabled"
                    self._redirect(
                        msg=f"Polling {status} for {device_id}", framed=is_framed
                    )
                    return
                if action == "reinitialize":
                    device_id = (data.get("id", [""])[0]).strip()
                    if not device_id:
                        raise ValueError("Missing device id")
                    if trigger_device_reinitialize is None:
                        raise ValueError("Device reinitialize not available")
                    trigger_device_reinitialize(device_id)
                    self._redirect(
                        msg=f"Reinitializing MQTT discovery for {device_id}",
                        framed=is_framed,
                    )
                    return
                if action == "delete":
                    device_id = (data.get("id", [""])[0]).strip()
                    if not device_id:
                        raise ValueError("Missing device id")
                    current = store.get(device_id)
                    if current is None:
                        raise ValueError(f"Device {device_id} not found")
                    if not store.delete(device_id):
                        raise ValueError(f"Device {device_id} not found")
                    # Clean up metrics for this device
                    if trigger_metrics_drop:
                        if current.device_uid:
                            trigger_metrics_drop(current.device_uid)
                        trigger_metrics_drop(device_id)
                    trigger_reload()
                    self._redirect(msg=f"Deleted device {device_id}", framed=is_framed)
                    return
                if action == "clear_metrics":
                    ok, message = _clear_metrics_scope()
                    if ok:
                        self._redirect(msg=message, framed=is_framed)
                    else:
                        self._redirect(err=message, framed=is_framed)
                    return
                raise ValueError(f"Unknown action: {action}")
            except (ValueError, TypeError, OSError) as err:
                self._redirect(err=str(err), framed=is_framed)

        def log_message(self, fmt: str, *args) -> None:
            LOG.info("%s - %s", self.client_address[0], fmt % args)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="ups2mqtt-web"
    )
    thread.start()
    LOG.info("Web UI listening on http://%s:%s", host, port)
    return server
