# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
import os
import threading
from logging.handlers import SysLogHandler
from pathlib import Path
from time import monotonic
from typing import Any

from .capability_repository import configure_capability_repository, get_capability_repository
from .capabilities import (
    bundled_source_keys,
    load_capabilities,
    poll_group_intervals,
    source_keys,
)
from .config import load_config, load_runtime_settings, save_runtime_settings
from .database import Database
from .ha_api import apply_entity_default_states, delete_device_entities
from .ha_api import delete_stale_ups_entities
from .log_buffer import BufferedLogHandler, LogBuffer
from .metrics import MetricsStore
from .model import DeviceConfig, ProfileConfig
from .mqtt import MqttPublisher
from .pollers import (
    get_idle_reconnect_seconds,
    get_metadata_refresh_interval_seconds,
    poll_device,
    set_idle_reconnect_seconds,
    set_metadata_refresh_interval_seconds,
)
from .store import DeviceStore
from .transforms import apply_catalog_transforms
from .web import start_web_server

LOG = logging.getLogger("ups2mqtt")
DEVICE_LOG = logging.getLogger("ups2mqtt.device")
AUDIT_LOG = logging.getLogger("ups2mqtt.audit")
DISCOVERY_MIGRATION_MARKER = "/data/.discovery_v2_migrated"


def _apply_catalog_derived_values(
    values: dict[str, Any],
    runtime_source: str,
    apps_dir: str | None,
    allowed_keys: set[str],
    raw_cache: dict[str, Any],
) -> None:
    """Derive bit-flag sensor values from raw polled registers using catalog metadata.

    The contract profile's register list (now augmented with legacy raw registers) will
    extract raw bitfield registers such as ups_status_bf and battery_system_error_bf.
    Catalog sensors marked source="derived" reference these raw registers via the
    notation "rawkey:bitN". This function resolves those derived values from either:
    1. Freshly polled raw register values (if present in this poll cycle), or
    2. Cached raw values from previous slow-poll cycles

    This allows derived sensors backed by slow-polled raw sources to remain stable
    between slow poll refreshes, preventing null flapping on fast poll cycles.

    Only derives keys that are in allowed_keys and not already present in values.
    Operates in-place on the values dict and updates raw_cache with newly polled values.
    """
    if not apps_dir or not runtime_source:
        return
    try:
        from .catalog import get_catalog_sensor_rows

        rows = get_catalog_sensor_rows(driver_key=runtime_source, apps_dir=apps_dir)
    except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
        return

    # Update cache with any freshly polled raw bitfield values
    raw_keys_to_cache = {
        "_bf",
        "status_word_",
        "error_bf",
        "fault_bf",
    }  # Common patterns for raw bitfield keys
    for k, v in values.items():
        if any(pattern in k for pattern in raw_keys_to_cache):
            raw_cache[k] = v

    derived_count = 0
    derived_from_cache = 0
    for row in rows:
        key = str(row.get("key", ""))
        if not key or key not in allowed_keys or key in values:
            continue
        source = str(row.get("source", ""))
        if source != "derived":
            continue
        reference = str(row.get("reference", ""))
        if ":" not in reference:
            continue
        # Parse "rawkey:bitN" notation
        parts = reference.split(":", 1)
        if len(parts) != 2:
            continue
        raw_key = parts[0].strip()
        bit_part = parts[1].strip()
        if not bit_part.startswith("bit"):
            continue
        try:
            bit_index = int(bit_part[3:])
        except (ValueError, IndexError):
            continue
        # Try fresh value first, then fall back to cached value
        raw_value = values.get(raw_key)
        used_cache = False
        if raw_value is None:
            raw_value = raw_cache.get(raw_key)
            used_cache = True
        if raw_value is None:
            continue
        try:
            raw_int = int(raw_value)
        except (TypeError, ValueError):
            continue
        values[key] = bool(raw_int & (1 << bit_index))
        derived_count += 1
        if used_cache:
            derived_from_cache += 1

    if derived_count:
        cache_msg = f", {derived_from_cache} from cache" if derived_from_cache else ""
        LOG.debug(
            "Catalog bit derivation: derived %d values%s for source=%s",
            derived_count,
            cache_msg,
            runtime_source,
        )


def _format_key_list(keys: list[str] | set[str], max_display: int = 10) -> str:
    """Format a list of keys for logging, truncating if too long."""
    sorted_keys = sorted(keys)
    total = len(sorted_keys)
    if total == 0:
        return "(none)"
    if total <= max_display:
        return ", ".join(sorted_keys)
    displayed = ", ".join(sorted_keys[:max_display])
    return f"{displayed} ... (+{total - max_display} more)"


def _sanitize_config_for_log(config_dict: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive fields from config dict for safe logging."""
    sanitized = config_dict.copy()
    sensitive_keys = {
        "password",
        "token",
        "secret",
        "key",
        "credential",
        "mqtt_password",
        "ha_token",
        "snmp_community",
    }
    for key in list(sanitized.keys()):
        if any(sensitive in key.lower() for sensitive in sensitive_keys):
            sanitized[key] = "***REDACTED***"
    return sanitized


def _configure_device_logger() -> None:
    """Keep device debug logging visible at INFO regardless of root logger level."""
    root_logger = logging.getLogger()
    DEVICE_LOG.handlers.clear()
    for handler in root_logger.handlers:
        DEVICE_LOG.addHandler(handler)
    DEVICE_LOG.setLevel(logging.INFO)
    DEVICE_LOG.propagate = False


def _configure_audit_syslog_handler() -> None:
    """Optionally forward maintenance audit logs to syslog."""
    enabled = os.environ.get("UPS_AUDIT_SYSLOG_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return
    host = os.environ.get("UPS_AUDIT_SYSLOG_HOST", "127.0.0.1").strip() or "127.0.0.1"
    raw_port = os.environ.get("UPS_AUDIT_SYSLOG_PORT", "514").strip() or "514"
    try:
        port = int(raw_port)
    except ValueError:
        LOG.warning(
            "Invalid UPS_AUDIT_SYSLOG_PORT=%s; audit syslog forwarding disabled",
            raw_port,
        )
        return
    handler = SysLogHandler(address=(host, port))
    handler.setFormatter(
        logging.Formatter(
            "ups2mqtt-audit[%(process)d]: %(name)s %(levelname)s %(message)s"
        )
    )
    AUDIT_LOG.addHandler(handler)
    AUDIT_LOG.setLevel(logging.INFO)
    AUDIT_LOG.propagate = True
    LOG.info("Audit syslog forwarding enabled: %s:%d", host, port)


def _emit_device_debug(device_id: str, values: dict[str, Any]) -> None:
    DEVICE_LOG.info(
        "Device debug [%s]: %s",
        device_id,
        json.dumps(values, sort_keys=True, default=str),
    )


def _maybe_emit_device_debug(
    enabled: bool, device_id: str, values: dict[str, Any]
) -> None:
    if not enabled:
        return
    _emit_device_debug(device_id, values)


async def _device_loop(
    device,
    runtime_source: str,
    profile: dict[str, Any],
    discovery_keys: list[str],
    mqtt: MqttPublisher,
    default_interval: int,
    poll_timeout: int,
    global_poll_semaphore: asyncio.Semaphore,
    endpoint_semaphores: dict[str, asyncio.Semaphore],
    metrics: MetricsStore,
    apps_dir: str | None = None,
) -> None:
    base_interval = device.poll_interval or default_interval
    base_interval = max(1, int(base_interval))
    group_intervals = poll_group_intervals(profile, base_interval)
    runtime_device = DeviceConfig(
        id=device.id,
        source=runtime_source,
        host=device.host,
        port=device.port,
        unit_id=device.unit_id,
        snmp_community=device.snmp_community,
        poll_interval=device.poll_interval,
        name=device.name,
        debug_logging=device.debug_logging,
        keep_connection_open=device.keep_connection_open,
        device_uid=device.device_uid,
        discovery_enabled=device.discovery_enabled,
        polling_enabled=device.polling_enabled,
        profile_uid=device.profile_uid,
        profile_mode=device.profile_mode,
        local_profile_payload=device.local_profile_payload,
        local_selected_sensors=device.local_selected_sensors,
        local_sensor_preferences=device.local_sensor_preferences,
        enable_extended_fields=device.enable_extended_fields,
    )
    if (
        not runtime_device.enable_extended_fields
        and _selected_keys_require_extended_fields(
            runtime_source=runtime_source,
            selected_keys=discovery_keys,
            apps_dir=apps_dir,
        )
    ):
        runtime_device.enable_extended_fields = True
        LOG.info(
            "Enabled extended polling for %s (%s) due to selected extended sensors",
            device.id,
            runtime_source,
        )
    allowed_keys = set(discovery_keys)
    LOG.debug(
        "Device loop for %s: allowed_keys initialized with %d keys: %s",
        device.id,
        len(allowed_keys),
        _format_key_list(list(allowed_keys), max_display=20),
    )
    now_started = monotonic()
    next_due: dict[str, float] = {
        group: now_started + float(interval)
        for group, interval in group_intervals.items()
    }
    startup_poll_sequence: list[list[str]] = []
    if "slow" in group_intervals:
        startup_poll_sequence.append(["slow"])
    if "fast" in group_intervals:
        startup_poll_sequence.append(["fast"])
    if startup_poll_sequence:
        LOG.info(
            "Startup poll warm-up for %s: %s",
            device.id,
            " -> ".join(",".join(groups) for groups in startup_poll_sequence),
        )
    endpoint_key = f"{device.host}:{device.port}"
    endpoint_semaphore = endpoint_semaphores.setdefault(
        endpoint_key, asyncio.Semaphore(1)
    )
    # Cache for raw bitfield register values from slow polls, used for deriving
    # catalog-derived sensors on fast poll cycles when raw sources aren't refreshed
    raw_bitfield_cache: dict[str, Any] = {}

    while True:
        now = monotonic()
        if startup_poll_sequence:
            due_groups = startup_poll_sequence.pop(0)
        else:
            due_groups = sorted(
                group for group, due_at in next_due.items() if due_at <= now
            )
        if not due_groups:
            sleep_for = min(max(0.1, min(next_due.values()) - now), 0.5)
            await asyncio.sleep(sleep_for)
            continue

        identity = device.device_uid or device.id
        started = monotonic()
        metrics.record_start(identity)
        try:
            LOG.debug(
                "Polling cycle started for %s (groups=%s, allowed_keys=%d)",
                device.id,
                ",".join(due_groups),
                len(allowed_keys),
            )
            async with global_poll_semaphore:
                async with endpoint_semaphore:
                    values = await asyncio.wait_for(
                        poll_device(runtime_device, profile, set(due_groups)),
                        timeout=max(2, poll_timeout),
                    )
            if values:
                # Derive bit-flag sensor values from raw polled registers using catalog
                # metadata. Uses cached raw values from slow polls when not freshly polled.
                # This must happen before the allowed_keys filter so that derived
                # keys (e.g. online_state from ups_status_bf:bit1) are included.
                _apply_catalog_derived_values(
                    values, runtime_source, apps_dir, allowed_keys, raw_bitfield_cache
                )
                values = apply_catalog_transforms(
                    values,
                    device_uid=identity,
                    runtime_source=runtime_source,
                    apps_dir=apps_dir,
                    value_cache=raw_bitfield_cache,
                )
                LOG.debug(
                    "Polling for %s returned %d values (after derivation): %s",
                    device.id,
                    len(values),
                    _format_key_list(list(values.keys()), max_display=15),
                )
                mqtt_values = {
                    key: value for key, value in values.items() if key in allowed_keys
                }
                filtered_count = len(values) - len(mqtt_values)
                if filtered_count > 0:
                    filtered_keys = set(values.keys()) - set(mqtt_values.keys())
                    LOG.debug(
                        "Polling for %s: filtered out %d keys not in allowed_keys: %s",
                        device.id,
                        filtered_count,
                        _format_key_list(list(filtered_keys), max_display=10),
                    )

                # Keys not returned in this cycle were not polled for this group mix
                # (e.g. slow-only keys during a fast cycle). Do not overwrite them
                # with null; let MQTT state cache preserve last-known values.
                not_polled = allowed_keys - set(values.keys())
                if not_polled:
                    LOG.debug(
                        "Device %s: %d allowed keys not polled this cycle; preserving previous state for: %s",
                        device.id,
                        len(not_polled),
                        _format_key_list(list(not_polled), max_display=10),
                    )

                published = mqtt.publish_state(
                    runtime_device,
                    mqtt_values,
                    discovery_keys=discovery_keys,
                )
                if published:
                    LOG.info(
                        "Published %d values for %s (polled=%d, filtered=%d)",
                        len(mqtt_values),
                        device.id,
                        len(values),
                        filtered_count,
                    )
                else:
                    LOG.info(
                        "Polled %d values for %s (MQTT disabled/offline, filtered=%d)",
                        len(mqtt_values),
                        device.id,
                        filtered_count,
                    )
                _maybe_emit_device_debug(
                    device.debug_logging,
                    device.id,
                    mqtt_values,
                )
                metrics.record_success(
                    identity, (monotonic() - started) * 1000, len(values)
                )
            else:
                LOG.warning("No values read for %s", device.id)
                metrics.record_success(identity, (monotonic() - started) * 1000, 0)
            LOG.info(
                "Polling cycle completed for %s in %.2fs",
                device.id,
                monotonic() - started,
            )
        except TimeoutError:
            LOG.error("Polling timeout for %s after %ss", device.id, poll_timeout)
            metrics.record_timeout(
                identity, (monotonic() - started) * 1000, poll_timeout
            )
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            LOG.exception("Polling failed for %s: %s", device.id, err)
            metrics.record_failure(identity, (monotonic() - started) * 1000, str(err))
        next_base = monotonic()
        for group in due_groups:
            next_due[group] = next_base + float(
                group_intervals.get(group, base_interval)
            )


async def _reconcile_device_tasks(
    profiles: dict[str, Any],
    profile_bindings: dict[str, ProfileConfig],
    store: DeviceStore,
    mqtt: MqttPublisher,
    default_interval: int,
    poll_timeout: int,
    global_poll_semaphore: asyncio.Semaphore,
    endpoint_semaphores: dict[str, asyncio.Semaphore],
    metrics: MetricsStore,
    discovery_registry: dict[str, set[str]],
    running: dict[str, tuple[DeviceConfig, str, asyncio.Task]],
    apps_dir: str | None = None,
    ha_url: str | None = None,
    ha_token: str | None = None,
) -> None:
    desired = {device.device_uid: device for device in store.list_devices()}

    for uid, (existing_device, existing_runtime_signature, task) in list(
        running.items()
    ):
        new_device = desired.get(uid)
        if new_device is None:
            task.cancel()
            mqtt.publish_unavailable(existing_device)
            known_keys = sorted(discovery_registry.pop(uid, set()))
            if known_keys:
                mqtt.clear_discovery(existing_device, known_keys)
            running.pop(uid, None)
            LOG.info("Removed device task for %s", existing_device.id)
            continue
        LOG.debug(
            "Profile resolution call site: _reconcile_device_tasks restart check (apps_dir=%s)",
            apps_dir,
        )
        (
            _existing_source,
            _existing_resolved_profile,
            _existing_discovery_keys,
            new_runtime_signature,
        ) = _resolve_runtime_profile(
            device=new_device,
            capability_profiles=profiles,
            profile_bindings=profile_bindings,
            apps_dir=apps_dir,
        )
        if (
            new_device.signature() != existing_device.signature()
            or new_runtime_signature != existing_runtime_signature
        ):
            task.cancel()
            running.pop(uid, None)
            config_changed = new_device.signature() != existing_device.signature()
            profile_changed = new_runtime_signature != existing_runtime_signature
            reason_parts = []
            if config_changed:
                reason_parts.append("device config")
            if profile_changed:
                reason_parts.append("profile/selection")
            LOG.info(
                "Restarting device task for %s due to %s change",
                new_device.id,
                " and ".join(reason_parts),
            )

    for uid, device in desired.items():
        LOG.debug(
            "Profile resolution call site: _reconcile_device_tasks device loop (apps_dir=%s)",
            apps_dir,
        )
        (
            runtime_source,
            runtime_profile,
            discovery_keys,
            runtime_signature,
        ) = _resolve_runtime_profile(
            device=device,
            capability_profiles=profiles,
            profile_bindings=profile_bindings,
            apps_dir=apps_dir,
        )
        if runtime_source not in profiles:
            LOG.error(
                "Skipping device %s: unknown source '%s'", device.id, device.source
            )
            continue
        runtime_device = _runtime_device_with_source(device, runtime_source)
        keys = list(discovery_keys)
        LOG.debug(
            "Device %s: discovery_keys from profile resolution=%d, keys list=%d: %s",
            device.id,
            len(discovery_keys),
            len(keys),
            _format_key_list(keys, max_display=20),
        )
        historical_keys = bundled_source_keys(runtime_source)
        current = set(keys)
        previous = discovery_registry.get(uid, set())
        removed = sorted(previous - current)
        if removed:
            LOG.debug(
                "Discovery for %s: removing %d stale keys: %s",
                device.id,
                len(removed),
                _format_key_list(removed, max_display=10),
            )
            mqtt.clear_discovery(runtime_device, removed)
            mqtt.clear_legacy_discovery(device.id, removed)
        stale_historical = sorted(set(historical_keys) - current)
        if stale_historical:
            LOG.debug(
                "Discovery for %s: removing %d stale historical keys: %s",
                device.id,
                len(stale_historical),
                _format_key_list(stale_historical, max_display=10),
            )
            mqtt.clear_discovery(runtime_device, stale_historical)
            mqtt.clear_legacy_discovery(device.id, stale_historical)

        if not device.discovery_enabled:
            if previous:
                LOG.info(
                    "Discovery disabled for %s: clearing %d existing entities",
                    device.id,
                    len(previous),
                )
                mqtt.clear_discovery(runtime_device, sorted(previous))
                discovery_registry.pop(uid, None)
            LOG.info(
                "Discovery disabled for %s; skipping discovery publish but keeping polling eligible",
                device.id,
            )
        else:
            mqtt.publish_discovery(
                runtime_device,
                keys,
            )
            discovery_registry[uid] = current
            if ha_url and ha_token:
                expected_defaults = {key: True for key in keys}
                if expected_defaults:
                    apply_result = await apply_entity_default_states(
                        ha_url=ha_url,
                        ha_token=ha_token,
                        device_identity=runtime_device.device_uid or runtime_device.id,
                        expected_defaults=expected_defaults,
                    )
                    if "error" in apply_result:
                        LOG.warning(
                            "HA default-state apply failed for %s: %s",
                            runtime_device.id,
                            apply_result.get("error"),
                        )
                    elif apply_result.get("failed"):
                        LOG.warning(
                            "HA default-state apply had %d failed updates for %s",
                            len(apply_result.get("failed", [])),
                            runtime_device.id,
                        )

        if not device.polling_enabled:
            mqtt.publish_unavailable(device)
            if uid in running:
                running[uid][2].cancel()
                running.pop(uid, None)
            continue

        if uid in running:
            continue
        task = asyncio.create_task(
            _device_loop(
                device,
                runtime_source,
                runtime_profile,
                keys,
                mqtt,
                default_interval,
                poll_timeout,
                global_poll_semaphore,
                endpoint_semaphores,
                metrics,
                apps_dir=apps_dir,
            )
        )
        running[uid] = (device, runtime_signature, task)
        LOG.info(
            "Started device task for %s (driver=%s, profile_mode=%s, discovery_keys=%d, polling=%s)",
            device.id,
            runtime_source,
            device.profile_mode,
            len(keys),
            "enabled" if device.polling_enabled else "disabled",
        )


def _runtime_device_with_source(device: DeviceConfig, source: str) -> DeviceConfig:
    return DeviceConfig(
        id=device.id,
        source=source,
        host=device.host,
        port=device.port,
        unit_id=device.unit_id,
        snmp_community=device.snmp_community,
        poll_interval=device.poll_interval,
        name=device.name,
        debug_logging=device.debug_logging,
        keep_connection_open=device.keep_connection_open,
        device_uid=device.device_uid,
        discovery_enabled=device.discovery_enabled,
        polling_enabled=device.polling_enabled,
        profile_uid=device.profile_uid,
        profile_mode=device.profile_mode,
        local_profile_payload=device.local_profile_payload,
        local_selected_sensors=device.local_selected_sensors,
        local_sensor_preferences=device.local_sensor_preferences,
        enable_extended_fields=device.enable_extended_fields,
    )


def _selected_keys_require_extended_fields(
    *,
    runtime_source: str,
    selected_keys: list[str],
    apps_dir: str | None,
) -> bool:
    if not runtime_source or not selected_keys:
        return False
    selected_set = {str(item) for item in selected_keys if str(item)}
    if not selected_set:
        return False

    if not apps_dir:
        return False
    try:
        from .catalog import get_catalog_sensor_rows

        rows = get_catalog_sensor_rows(driver_key=runtime_source, apps_dir=apps_dir)
    except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
        return False
    for row in rows:
        key = str(row.get("key", ""))
        if key not in selected_set:
            continue
        if str(row.get("tier", "normalized")) == "extended":
            return True
    return False


def _resolve_runtime_profile(
    *,
    device: DeviceConfig,
    capability_profiles: dict[str, Any],
    profile_bindings: dict[str, ProfileConfig],
    apps_dir: str | None = None,
) -> tuple[str, dict[str, Any], list[str], dict[str, bool], str]:
    runtime_source = device.source
    binding = profile_bindings.get(device.profile_uid) if device.profile_uid else None
    profile_mode = str(device.profile_mode).lower()

    if binding is not None:
        runtime_source = binding.driver_key
        LOG.debug(
            "Profile resolution for %s: using profile '%s' (mode=%s, driver=%s)",
            device.id,
            binding.name if hasattr(binding, "name") else device.profile_uid,
            profile_mode,
            runtime_source,
        )
    else:
        LOG.debug(
            "Profile resolution for %s: no profile binding (driver=%s)",
            device.id,
            runtime_source,
        )

    base_profile = capability_profiles.get(runtime_source)
    if not isinstance(base_profile, dict):
        LOG.warning(
            "Profile resolution for %s: driver '%s' not found in capabilities",
            device.id,
            runtime_source,
        )
        return runtime_source, {}, [], {}, f"{runtime_source}|missing"

    effective_profile = deepcopy(base_profile)
    if binding is not None:
        payload = (
            binding.config_payload
            if str(device.profile_mode).lower() != "local"
            else (device.local_profile_payload or binding.config_payload)
        )
        if isinstance(payload, dict):
            _apply_profile_payload_overrides(
                effective_profile=effective_profile,
                payload=payload,
            )

    # Get contract sensor keys
    available_keys = [
        key
        for item in source_keys(effective_profile)
        for key in [str(item).strip()]
        if key and not key.lower().endswith("_bf")
    ]
    available_set = set(available_keys)
    contract_count = len(available_keys)

    # Add catalog sensor keys if available
    catalog_count = 0
    if not apps_dir:
        LOG.warning(
            "Profile resolution called without apps_dir for %s (driver=%s) - catalog sensors unavailable",
            device.id,
            runtime_source,
        )
    if apps_dir and runtime_source:
        LOG.debug(
            "Profile resolution for %s: attempting catalog load (driver=%s, apps_dir=%s)",
            device.id,
            runtime_source,
            apps_dir,
        )
        try:
            from .catalog import get_catalog_derived_metrics, get_catalog_keys

            catalog_keys = get_catalog_keys(
                driver_key=runtime_source,
                apps_dir=apps_dir,
            )
            catalog_count = len(catalog_keys)
            added_count = 0
            for key in catalog_keys:
                if key and key not in available_set:
                    available_keys.append(key)
                    available_set.add(key)
                    added_count += 1
            LOG.debug(
                "Profile resolution for %s: catalog loaded %d keys, added %d new (contract=%d, total=%d)",
                device.id,
                catalog_count,
                added_count,
                contract_count,
                len(available_set),
            )
            derived = get_catalog_derived_metrics(
                driver_key=runtime_source,
                apps_dir=apps_dir,
            )
            derived_added = 0
            for declaration in derived:
                if not isinstance(declaration, dict):
                    continue
                output_key = str(declaration.get("output_key", "")).strip()
                if (
                    output_key
                    and output_key not in available_set
                    and not output_key.lower().endswith("_bf")
                ):
                    available_keys.append(output_key)
                    available_set.add(output_key)
                    derived_added += 1
            if derived_added:
                LOG.debug(
                    "Profile resolution for %s: added %d transform output keys",
                    device.id,
                    derived_added,
                )
        except (ImportError, ValueError) as err:
            LOG.warning(
                "Profile resolution for %s: catalog unavailable for %s (%s), using contract keys only",
                device.id,
                runtime_source,
                err,
            )
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            LOG.error(
                "Profile resolution for %s: unexpected catalog error for %s: %s",
                device.id,
                runtime_source,
                err,
                exc_info=True,
            )
    else:
        LOG.debug(
            "Profile resolution for %s: catalog skipped (apps_dir=%s, runtime_source=%s)",
            device.id,
            apps_dir,
            runtime_source,
        )
    sensor_preferences: dict[str, dict[str, Any]] = {}
    if binding is not None:
        if isinstance(binding.sensor_preferences, dict):
            sensor_preferences = {
                str(key): (
                    {
                        "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                        **(
                            {"poll_group": str(values.get("poll_group", "")).strip()}
                            if str(values.get("poll_group", "")).strip()
                            else {}
                        ),
                    }
                )
                for key, values in binding.sensor_preferences.items()
                if str(key) and isinstance(values, dict)
            }
        selected = (
            binding.selected_sensors
            if str(device.profile_mode).lower() != "local"
            else (
                device.local_selected_sensors
                if device.local_selected_sensors is not None
                else binding.selected_sensors
            )
        )
        if str(device.profile_mode).lower() == "local" and isinstance(
            device.local_sensor_preferences, dict
        ):
            sensor_preferences = {
                str(key): (
                    {
                        "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                        **(
                            {"poll_group": str(values.get("poll_group", "")).strip()}
                            if str(values.get("poll_group", "")).strip()
                            else {}
                        ),
                    }
                )
                for key, values in device.local_sensor_preferences.items()
                if str(key) and isinstance(values, dict)
            }
        raw_selected_count = len(selected) if selected else 0
        selected_keys = [str(item) for item in selected if str(item) in available_set]
        filtered_out_count = raw_selected_count - len(selected_keys)
        if filtered_out_count > 0:
            LOG.debug(
                "Profile resolution for %s: %d selected sensors filtered (not in available_set)",
                device.id,
                filtered_out_count,
            )
    elif device.local_selected_sensors is not None:
        raw_selected_count = len(device.local_selected_sensors)
        selected_keys = [
            str(item)
            for item in device.local_selected_sensors
            if str(item) in available_set
        ]
        filtered_out_count = raw_selected_count - len(selected_keys)
        if filtered_out_count > 0:
            LOG.debug(
                "Profile resolution for %s: %d local selected sensors filtered (not in available_set)",
                device.id,
                filtered_out_count,
            )
        if isinstance(device.local_sensor_preferences, dict):
            sensor_preferences = {
                str(key): (
                    {
                        "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                        **(
                            {"poll_group": str(values.get("poll_group", "")).strip()}
                            if str(values.get("poll_group", "")).strip()
                            else {}
                        ),
                    }
                )
                for key, values in device.local_sensor_preferences.items()
                if str(key) and isinstance(values, dict)
            }
    else:
        selected_keys = available_keys
        LOG.debug(
            "Profile resolution for %s: no explicit selection, using all %d available keys",
            device.id,
            len(available_keys),
        )

    sensor_poll_overrides = _apply_sensor_poll_group_overrides(
        effective_profile=effective_profile,
        sensor_preferences=sensor_preferences,
        runtime_source=runtime_source,
    )
    if sensor_poll_overrides > 0:
        LOG.debug(
            "Profile resolution for %s: applied %d poll_group sensor overrides",
            device.id,
            sensor_poll_overrides,
        )

    # Apply sensor preferences (mqtt_enabled filtering)
    selected_before_prefs = len(selected_keys)
    if sensor_preferences:
        selected_set = set(selected_keys)
        selected_keys = [
            key
            for key in available_keys
            if bool(
                sensor_preferences.get(key, {}).get(
                    "mqtt_enabled",
                    key in selected_set,
                )
            )
        ]
        mqtt_disabled_count = selected_before_prefs - len(selected_keys)
        if mqtt_disabled_count > 0:
            LOG.debug(
                "Profile resolution for %s: %d sensors excluded due to mqtt_enabled=false",
                device.id,
                mqtt_disabled_count,
            )
    mqtt_disabled_count = selected_before_prefs - len(selected_keys)
    if sensor_preferences:
        LOG.debug(
            "Profile resolution for %s: after mqtt_enabled filter: selected_keys=%d (was %d), filtered_out=%d",
            device.id,
            len(selected_keys),
            selected_before_prefs,
            mqtt_disabled_count,
        )

    LOG.info(
        "Profile resolution for %s: resolved %d discovery keys (contract=%d, catalog=%d, mqtt_disabled=%d)",
        device.id,
        len(selected_keys),
        contract_count,
        catalog_count,
        mqtt_disabled_count if sensor_preferences else 0,
    )
    LOG.debug(
        "Profile resolution for %s: discovery keys: %s",
        device.id,
        _format_key_list(selected_keys, max_display=15),
    )

    signature_payload = {
        "source": runtime_source,
        "profile_uid": device.profile_uid,
        "profile_mode": device.profile_mode,
        "effective_profile": effective_profile,
        "selected_keys": selected_keys,
    }
    signature = json.dumps(signature_payload, sort_keys=True, default=str)
    return (
        runtime_source,
        effective_profile,
        selected_keys,
        signature,
    )


def _apply_profile_payload_overrides(
    *,
    effective_profile: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    poll_groups = payload.get("poll_groups")
    if isinstance(poll_groups, dict):
        profile_groups = effective_profile.get("poll_groups")
        if isinstance(profile_groups, dict):
            for name, interval in poll_groups.items():
                if name not in profile_groups:
                    continue
                if not isinstance(profile_groups.get(name), dict):
                    continue
                try:
                    interval_int = max(1, int(interval))
                except (TypeError, ValueError):
                    continue
                profile_groups[name]["interval_s"] = interval_int

    key_precedence = payload.get("key_precedence")
    if isinstance(key_precedence, dict):
        profile_precedence = effective_profile.get("key_precedence")
        if isinstance(profile_precedence, dict):
            for metric_key, source_name in key_precedence.items():
                source_text = str(source_name).lower()
                if metric_key in profile_precedence and source_text in {
                    "modbus",
                    "snmp",
                }:
                    profile_precedence[str(metric_key)] = source_text


def _apply_sensor_poll_group_overrides(
    *,
    effective_profile: dict[str, Any],
    sensor_preferences: dict[str, dict[str, Any]],
    runtime_source: str | None = None,
) -> int:
    poll_groups = effective_profile.get("poll_groups", {})
    if not isinstance(poll_groups, dict):
        return 0
    interval_by_group: dict[str, int] = {}
    for name, spec in poll_groups.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        try:
            interval_by_group[name] = max(1, int(spec.get("interval_s", 60)))
        except (TypeError, ValueError):
            continue
    if not interval_by_group:
        return 0

    overrides: dict[str, str] = {}
    for key, values in sensor_preferences.items():
        if not isinstance(key, str) or not isinstance(values, dict):
            continue
        if not bool(values.get("mqtt_enabled", True)):
            continue
        group = str(values.get("poll_group", "")).strip()
        if group in interval_by_group:
            overrides[key] = group
    if not overrides:
        return 0

    # Expand poll-group overrides across canonical/alias equivalents from catalog
    # so profile selections expressed in canonical keys affect raw transport keys.
    expanded_overrides = dict(overrides)
    if runtime_source:
        try:
            specs = get_capability_repository().load_catalog_sensor_specs(runtime_source)
        except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            specs = []
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            canonical = str(spec.get("key", "")).strip()
            aliases_raw = spec.get("aliases", [])
            aliases = (
                [str(item).strip() for item in aliases_raw if str(item).strip()]
                if isinstance(aliases_raw, list)
                else []
            )
            names = [name for name in [canonical, *aliases] if name]
            if len(names) < 2:
                continue
            chosen_group = ""
            for name in names:
                group = expanded_overrides.get(name, "")
                if group:
                    chosen_group = group
                    break
            if not chosen_group:
                continue
            for name in names:
                expanded_overrides.setdefault(name, chosen_group)

    updated = 0

    def _apply_to_registers(registers: Any) -> None:
        nonlocal updated
        if not isinstance(registers, list):
            return
        for item in registers:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            group = expanded_overrides.get(key)
            if not group:
                continue
            if str(item.get("poll_group", "slow")) != group:
                item["poll_group"] = group
                updated += 1

    def _apply_to_oids(oids: Any) -> None:
        nonlocal updated
        if not isinstance(oids, dict):
            return
        for key, spec in oids.items():
            if not isinstance(spec, dict):
                continue
            group = expanded_overrides.get(str(key))
            if not group:
                continue
            if str(spec.get("poll_group", "slow")) != group:
                spec["poll_group"] = group
                updated += 1

    def _apply_to_blocks(blocks: Any) -> None:
        nonlocal updated
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            metrics = block.get("metrics", [])
            if not isinstance(metrics, list):
                continue
            block_groups = {
                expanded_overrides.get(str(metric).strip(), "")
                for metric in metrics
                if str(metric).strip()
            }
            block_groups = {group for group in block_groups if group}
            if not block_groups:
                continue
            if len(block_groups) == 1:
                chosen = next(iter(block_groups))
            else:
                chosen = min(
                    block_groups,
                    key=lambda name: interval_by_group.get(name, 60),
                )
            if str(block.get("poll_group", "slow")) != chosen:
                block["poll_group"] = chosen
                updated += 1

    _apply_to_registers(effective_profile.get("registers", []))
    _apply_to_blocks(effective_profile.get("register_blocks", []))
    _apply_to_oids(effective_profile.get("oids", {}))
    for transport in ("modbus", "snmp"):
        section = effective_profile.get(transport, {})
        if not isinstance(section, dict):
            continue
        _apply_to_registers(section.get("registers", []))
        _apply_to_blocks(section.get("register_blocks", []))
        _apply_to_oids(section.get("oids", {}))
        _apply_to_blocks(section.get("snmp_blocks", []))
    return updated


async def async_main() -> None:
    # Default to INFO for better observability, can be overridden by environment
    log_level_name = os.environ.get("UPS_UNIFIED_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    _configure_audit_syslog_handler()
    config = load_config()
    os.environ.setdefault("UPS_UNIFIED_APPS_DIR", config.apps_dir)
    runtime_settings = load_runtime_settings()
    runtime_settings_state: dict[str, Any] = {
        "timezone": str(runtime_settings.get("timezone", "UTC")).strip() or "UTC",
        "theme": (
            str(runtime_settings.get("theme", "system")).strip().lower()
            if str(runtime_settings.get("theme", "system")).strip().lower()
            in {"light", "dark", "system"}
            else "system"
        ),
        "metadata_refresh_interval_seconds": max(
            1,
            int(
                runtime_settings.get(
                    "metadata_refresh_interval_seconds",
                    get_metadata_refresh_interval_seconds(),
                )
            ),
        ),
        "idle_reconnect_seconds": max(
            1.0,
            float(
                runtime_settings.get(
                    "idle_reconnect_seconds",
                    get_idle_reconnect_seconds(),
                )
            ),
        ),
    }
    runtime_settings_lock = threading.Lock()

    def _persist_runtime_settings() -> None:
        save_runtime_settings(dict(runtime_settings_state))

    def _get_timezone() -> str:
        with runtime_settings_lock:
            return str(runtime_settings_state["timezone"])

    def _set_timezone(name: str) -> None:
        with runtime_settings_lock:
            runtime_settings_state["timezone"] = name
            _persist_runtime_settings()

    def _get_theme() -> str:
        with runtime_settings_lock:
            return str(runtime_settings_state["theme"])

    def _set_theme(name: str) -> None:
        with runtime_settings_lock:
            runtime_settings_state["theme"] = (
                name if name in {"light", "dark", "system"} else "system"
            )
            _persist_runtime_settings()

    def _get_metadata_refresh_interval_seconds() -> int:
        with runtime_settings_lock:
            return int(runtime_settings_state["metadata_refresh_interval_seconds"])

    def _set_metadata_refresh_interval_seconds(seconds: int) -> None:
        seconds_int = max(1, int(seconds))
        set_metadata_refresh_interval_seconds(seconds_int)
        with runtime_settings_lock:
            runtime_settings_state["metadata_refresh_interval_seconds"] = seconds_int
            _persist_runtime_settings()

    def _get_idle_reconnect_seconds() -> float:
        with runtime_settings_lock:
            return float(runtime_settings_state["idle_reconnect_seconds"])

    def _set_idle_reconnect_seconds(seconds: float) -> None:
        seconds_value = max(1.0, float(seconds))
        set_idle_reconnect_seconds(seconds_value)
        with runtime_settings_lock:
            runtime_settings_state["idle_reconnect_seconds"] = seconds_value
            _persist_runtime_settings()

    # Apply persisted runtime timer settings at startup.
    _set_metadata_refresh_interval_seconds(
        int(runtime_settings_state["metadata_refresh_interval_seconds"])
    )
    _set_idle_reconnect_seconds(float(runtime_settings_state["idle_reconnect_seconds"]))

    log_buffer = LogBuffer()
    buffered_handler = BufferedLogHandler(log_buffer)
    logging.getLogger().addHandler(buffered_handler)
    _configure_device_logger()

    # Log startup configuration summary
    LOG.info("=== ups2mqtt starting ===")
    LOG.info("Configuration summary:")
    LOG.info("  apps_dir: %s", config.apps_dir)
    LOG.info("  poll_interval: %ds", config.poll_interval)
    LOG.info("  poll_timeout: %ds", config.poll_timeout)
    LOG.info("  max_concurrent_polls: %d", config.max_concurrent_polls)
    LOG.info("  mqtt_enabled: %s", config.mqtt_enabled)
    if config.mqtt_enabled:
        LOG.info("  mqtt_host: %s", config.mqtt_host)
        LOG.info("  mqtt_port: %d", config.mqtt_port)
    LOG.info("  web_enabled: %s", config.web_enabled)
    if config.web_enabled:
        LOG.info("  web_port: %d", config.web_port)
    LOG.info("  ha_url: %s", config.ha_url if config.ha_url else "(not configured)")
    LOG.info(
        "  log_level: %s", logging.getLevelName(logging.getLogger().getEffectiveLevel())
    )

    # Check apps_dir existence
    apps_dir_path = Path(config.apps_dir)
    if apps_dir_path.exists():
        subdirs = [d.name for d in apps_dir_path.iterdir() if d.is_dir()]
        LOG.info(
            "  apps_dir exists with %d subdirectories: %s",
            len(subdirs),
            ", ".join(subdirs),
        )
    else:
        LOG.warning("  apps_dir does not exist: %s", config.apps_dir)

    # Initialize database
    db = Database()
    configure_capability_repository(db)

    all_caps = load_capabilities()
    LOG.info("Capabilities source: %s", all_caps.get("source", "bundled"))
    for err in all_caps.get("validation_errors", []):
        LOG.warning("Capabilities validation: %s", err)
    profile_state: dict[str, Any] = {
        "source": str(all_caps.get("source", "bundled")),
        "profiles": all_caps["profiles"],
    }

    mqtt = MqttPublisher(config)
    mqtt.connect()
    if config.mqtt_enabled:
        LOG.info("MQTT target configured: %s:%s", config.mqtt_host, config.mqtt_port)
    else:
        LOG.info("MQTT is disabled; polling + web UI only")

    # Load devices from database, fallback to config if database is empty
    db_devices = db.load_devices()
    devices_to_use = db_devices if db_devices else config.devices

    # Save config devices to database if database was empty
    if not db_devices and config.devices:
        for device in config.devices:
            db.save_device(device)

    store = DeviceStore(devices_to_use, db)
    loop = asyncio.get_running_loop()
    global_poll_semaphore = asyncio.Semaphore(config.max_concurrent_polls)
    metrics = MetricsStore(global_poll_semaphore=global_poll_semaphore, db=db)

    # Helper for mapping metrics IDs to device IDs when names change
    def _metrics_rename(old_id: str, new_id: str) -> None:
        metrics.rename(old_id, new_id)

    def _metrics_drop(device_id: str) -> None:
        metrics.drop(device_id)

    def _metrics_prune_to_store_devices() -> None:
        valid_ids: set[str] = set()
        for device in store.list_devices():
            if device.id:
                valid_ids.add(device.id)
            if device.device_uid:
                valid_ids.add(device.device_uid)
        removed = metrics.prune_unknown(valid_ids)
        if removed:
            LOG.info("Pruned %d stale metrics rows", removed)

    def _metrics_clear() -> None:
        metrics.clear_all()

    def _cleanup_db_state() -> dict[str, int]:
        devices = store.list_devices()
        valid_uids = {device.device_uid for device in devices if device.device_uid}
        db_result = db.cleanup_state(valid_device_uids=valid_uids)
        valid_metric_keys = valid_uids | {device.id for device in devices if device.id}
        metrics_removed_mem = metrics.prune_unknown(valid_metric_keys)
        result = dict(db_result)
        result["metrics_removed_memory"] = metrics_removed_mem
        LOG.info(
            "Manual DB cleanup complete: devices_removed=%d metrics_removed_memory=%d",
            result.get("devices_removed", 0),
            metrics_removed_mem,
        )
        return result

    reload_event = asyncio.Event()
    republish_discovery_event = asyncio.Event()
    capability_reload_event = asyncio.Event()
    device_reinitialize_queue: asyncio.Queue[str] = (
        asyncio.Queue()
    )  # device IDs to reinitialize
    endpoint_semaphores: dict[str, asyncio.Semaphore] = {}

    web_server = None
    if config.web_enabled:
        web_server = start_web_server(
            host=config.web_host,
            port=config.web_port,
            store=store,
            get_source_names=lambda: sorted(profile_state["profiles"].keys()),
            log_buffer=log_buffer,
            get_capability_status=lambda: {
                "source": str(profile_state.get("source", "unknown")),
                "profile_count": len(profile_state.get("profiles", {})),
                "max_concurrent_polls": config.max_concurrent_polls,
                "apps_dir": config.apps_dir,
            },
            get_capability_profiles=lambda: profile_state.get("profiles", {}),
            trigger_capability_reload=lambda: loop.call_soon_threadsafe(
                capability_reload_event.set
            ),
            trigger_republish_discovery=lambda: loop.call_soon_threadsafe(
                republish_discovery_event.set
            ),
            get_metrics_snapshot=metrics.snapshot,
            trigger_reload=lambda: loop.call_soon_threadsafe(reload_event.set),
            trigger_metrics_drop=_metrics_drop,
            trigger_metrics_clear=_metrics_clear,
            trigger_db_cleanup=_cleanup_db_state,
            trigger_device_reinitialize=lambda device_id: loop.call_soon_threadsafe(
                device_reinitialize_queue.put_nowait, device_id
            ),
            get_config=lambda: config,
            get_timezone=_get_timezone,
            set_timezone=_set_timezone,
            get_theme=_get_theme,
            set_theme=_set_theme,
            get_metadata_refresh_interval_seconds=_get_metadata_refresh_interval_seconds,
            set_metadata_refresh_interval_seconds=_set_metadata_refresh_interval_seconds,
            get_idle_reconnect_seconds=_get_idle_reconnect_seconds,
            set_idle_reconnect_seconds=_set_idle_reconnect_seconds,
            get_cached_ha_payload_preview=mqtt.get_cached_ha_payload_preview,
        )

    running: dict[str, tuple[DeviceConfig, str, asyncio.Task]] = {}
    discovery_registry: dict[str, set[str]] = {}
    try:
        profile_bindings = {
            item.profile_uid: item for item in db.load_profiles() if item.profile_uid
        }
        _run_discovery_migration_cleanup(
            mqtt=mqtt,
            devices=store.list_devices(),
            profiles=profile_state["profiles"],
            profile_bindings=profile_bindings,
            apps_dir=config.apps_dir,
        )
        reload_event.set()
        while True:
            if capability_reload_event.is_set():
                capability_reload_event.clear()
                try:
                    caps = load_capabilities()
                    profile_state["source"] = str(caps.get("source", "bundled"))
                    profile_state["profiles"] = caps["profiles"]
                    for err in caps.get("validation_errors", []):
                        LOG.warning("Capabilities validation: %s", err)
                    LOG.info(
                        "Capabilities reloaded: source=%s profiles=%d",
                        profile_state["source"],
                        len(profile_state["profiles"]),
                    )
                    reload_event.set()
                except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                    LOG.exception("Capability reload failed: %s", err)
            if republish_discovery_event.is_set():
                republish_discovery_event.clear()
                LOG.info("Manual MQTT discovery republish requested")
                reload_event.set()
            # Handle per-device reinitialize requests
            try:
                device_id = device_reinitialize_queue.get_nowait()
                device = store.get(device_id)
                if device is not None:
                    LOG.info("Reinitializing MQTT discovery for %s", device_id)
                    profile_bindings = {
                        item.profile_uid: item
                        for item in db.load_profiles()
                        if item.profile_uid
                    }

                    LOG.debug(
                        "Profile resolution call site: device_reinitialize_handler (apps_dir=%s)",
                        config.apps_dir,
                    )
                    (
                        runtime_source,
                        runtime_profile,
                        keys,
                        _,
                    ) = _resolve_runtime_profile(
                        device=device,
                        capability_profiles=profile_state["profiles"],
                        profile_bindings=profile_bindings,
                        apps_dir=config.apps_dir,
                    )
                    runtime_device = _runtime_device_with_source(device, runtime_source)
                    if runtime_profile:
                        if keys:
                            purge_keys = sorted(
                                set(keys) | set(bundled_source_keys(runtime_source))
                            )
                            # Clear retained discovery first to avoid HA reusing stale entries.
                            mqtt.clear_discovery(runtime_device, purge_keys)

                            # Delete old HA entity-registry rows if HA is configured.
                            if config.ha_url and config.ha_token:
                                deletion_identity = device.device_uid or device.id
                                deletion_result = await delete_device_entities(
                                    config.ha_url, config.ha_token, deletion_identity
                                )
                                if deletion_result.get("skipped"):
                                    LOG.debug(
                                        "HA deletion skipped: %s",
                                        deletion_result.get("reason"),
                                    )
                                elif "error" in deletion_result:
                                    LOG.warning(
                                        "HA entity deletion failed: %s",
                                        deletion_result["error"],
                                    )
                                elif "deleted" in deletion_result:
                                    deleted = deletion_result["deleted"]
                                    LOG.info(
                                        "Deleted %d HA entities for device %s",
                                        len(deleted),
                                        device_id,
                                    )

                            mqtt.publish_discovery(
                                runtime_device,
                                keys,
                            )
                            if config.ha_url and config.ha_token:
                                defaults = {key: True for key in keys}
                                apply_result = await apply_entity_default_states(
                                    config.ha_url,
                                    config.ha_token,
                                    device.device_uid or device.id,
                                    defaults,
                                )
                                if apply_result.get("skipped"):
                                    LOG.debug(
                                        "HA defaults apply skipped: %s",
                                        apply_result.get("reason"),
                                    )
                                elif "error" in apply_result:
                                    LOG.warning(
                                        "HA defaults apply failed: %s",
                                        apply_result["error"],
                                    )
                                else:
                                    updated = apply_result.get("updated", [])
                                    failed = apply_result.get("failed", [])
                                    LOG.info(
                                        "Applied HA entity defaults for %s: updated=%d failed=%d",
                                        device_id,
                                        len(updated),
                                        len(failed),
                                    )
                            LOG.info(
                                "Reinitialized discovery for %s: cleared and republished %d metrics",
                                device_id,
                                len(keys),
                            )
                        else:
                            LOG.warning(
                                "No metric keys available for profile %s", device.source
                            )
                    else:
                        LOG.warning(
                            "Unknown source %s for device %s", device.source, device_id
                        )
                else:
                    LOG.warning("Device %s not found in store", device_id)
            except asyncio.QueueEmpty:
                pass
            if reload_event.is_set():
                reload_event.clear()
                profile_bindings = {
                    item.profile_uid: item
                    for item in db.load_profiles()
                    if item.profile_uid
                }
                await _reconcile_device_tasks(
                    profiles=profile_state["profiles"],
                    profile_bindings=profile_bindings,
                    store=store,
                    mqtt=mqtt,
                    default_interval=config.poll_interval,
                    poll_timeout=config.poll_timeout,
                    global_poll_semaphore=global_poll_semaphore,
                    endpoint_semaphores=endpoint_semaphores,
                    metrics=metrics,
                    discovery_registry=discovery_registry,
                    running=running,
                    apps_dir=config.apps_dir,
                    ha_url=config.ha_url,
                    ha_token=config.ha_token,
                )
                _metrics_prune_to_store_devices()
                await _prune_stale_ha_entities(
                    config=config,
                    devices=store.list_devices(),
                    profiles=profile_state["profiles"],
                    profile_bindings=profile_bindings,
                )
            await asyncio.sleep(1)
    finally:
        for device, _runtime_signature, task in running.values():
            task.cancel()
            mqtt.publish_unavailable(device)
        if web_server is not None:
            web_server.shutdown()
            web_server.server_close()
        mqtt.close()
        logging.getLogger().removeHandler(buffered_handler)


def main() -> None:
    asyncio.run(async_main())

def _run_discovery_migration_cleanup(
    mqtt: MqttPublisher,
    devices: list[DeviceConfig],
    profiles: dict[str, Any],
    profile_bindings: dict[str, ProfileConfig],
    apps_dir: str | None = None,
) -> None:
    marker = Path(DISCOVERY_MIGRATION_MARKER)
    if marker.exists():
        return
    if not mqtt.ensure_connected():
        return
    for device in devices:
        if not device.discovery_enabled:
            continue
        LOG.debug(
            "Profile resolution call site: _run_discovery_migration_cleanup (apps_dir=%s)",
            apps_dir,
        )
        runtime_source, runtime_profile, keys, _ = (
            _resolve_runtime_profile(
                device=device,
                capability_profiles=profiles,
                profile_bindings=profile_bindings,
                apps_dir=apps_dir,
            )
        )
        if not isinstance(runtime_profile, dict):
            continue
        runtime_device = _runtime_device_with_source(device, runtime_source)
        if keys:
            mqtt.clear_legacy_discovery(runtime_device.id, keys)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("done\n", encoding="utf-8")
    LOG.info("Completed one-time legacy discovery cleanup migration")


async def _prune_stale_ha_entities(
    config,
    devices: list[DeviceConfig],
    profiles: dict[str, Any],
    profile_bindings: dict[str, ProfileConfig],
) -> None:
    if not config.ha_url or not config.ha_token:
        return

    expected_unique_ids: set[str] = {"ups_unified_bridge"}
    expected_device_identifiers: set[str] = {"ups_unified_bridge"}
    for device in devices:
        if not device.discovery_enabled:
            continue
        LOG.debug(
            "Profile resolution call site: _prune_stale_ha_entities (apps_dir=%s)",
            config.apps_dir,
        )
        runtime_source, runtime_profile, keys, _ = (
            _resolve_runtime_profile(
                device=device,
                capability_profiles=profiles,
                profile_bindings=profile_bindings,
                apps_dir=config.apps_dir,
            )
        )
        if not isinstance(runtime_profile, dict):
            continue
        identity = device.device_uid or device.id
        expected_device_identifiers.add(f"ups_unified_{identity}")
        for key in keys:
            expected_unique_ids.add(f"ups_unified_{identity}_{key}")

    result = await delete_stale_ups_entities(
        config.ha_url,
        config.ha_token,
        expected_unique_ids,
        expected_device_identifiers,
    )
    if result.get("skipped"):
        LOG.debug("HA stale prune skipped: %s", result.get("reason"))
    elif "error" in result:
        LOG.warning("HA stale prune failed: %s", result["error"])
    else:
        deleted = result.get("deleted", [])
        removed_devices = result.get("removed_devices", [])
        if deleted or removed_devices:
            LOG.info(
                "Removed stale HA registry entries: entities=%d devices=%d",
                len(deleted),
                len(removed_devices),
            )


if __name__ == "__main__":
    main()
