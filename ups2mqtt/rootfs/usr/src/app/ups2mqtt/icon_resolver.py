# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""Dynamically load and use icon mappings from external HACS app projects."""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Callable

from .drivers.runtime_metadata import driver_owns_runtime_metadata

LOG = logging.getLogger("ups2mqtt.icon_resolver")
LOCAL_CONTRACTS_DIR = Path(
    os.environ.get("UPS_UNIFIED_LOCAL_CONTRACTS_DIR", "/usr/src/app/contracts")
)

# Cache loaded modules to avoid repeated imports
_module_cache: dict[str, object] = {}
_all_disabled_defaults_warned_sources: set[str] = set()

# Fallback icon mappings for when external apps can't be imported
# These match the icon functions in the external apps
FALLBACK_ICONS: dict[str, dict[str, str]] = {
    "apc_modbus": {
        "temperature": "mdi:thermometer",
        "humidity": "mdi:water-percent",
        "runtime": "mdi:timer-outline",
        "delay": "mdi:timer-outline",
        "duration": "mdi:timer-outline",
        "energy": "mdi:meter-electric",
        "apparent_power": "mdi:flash-outline",
        "real_power": "mdi:flash",
        "reactive_power": "mdi:flash",
        "active_power": "mdi:flash",
        "power": "mdi:flash",
        "power_factor": "mdi:angle-acute",
        "current": "mdi:current-ac",
        "amps": "mdi:current-ac",
        "amperage": "mdi:current-ac",
        "frequency": "mdi:sine-wave",
        "voltage": "mdi:sine-wave",
        "volt": "mdi:sine-wave",
        "transfer_point": "mdi:sine-wave",
        "battery": "mdi:battery-medium",
        "battery_voltage": "mdi:battery",
        "battery_capacity": "mdi:battery",
        "battery_charge": "mdi:battery",
        "state_of_charge": "mdi:battery",
        "load": "mdi:gauge",
        "buzzer_muted": "mdi:volume-off",
        "input_fail": "mdi:alert-circle",
        "bypass_fail": "mdi:alert-circle",
        "general_error": "mdi:alert-circle",
        "inverter_off": "mdi:power",
        "load_on_source": "mdi:power-plug",
        "no_output": "mdi:power-plug-off",
        "output_off": "mdi:power-plug-off",
        "output_disabled": "mdi:power-plug-off",
        "output_shorted": "mdi:flash-alert",
        "overload": "mdi:car-brake-alert",
        "bypass_overload": "mdi:car-brake-alert",
        "bypass": "mdi:transit-detour",
        "line_count": "mdi:transmission-tower",
        "phase_count": "mdi:transmission-tower",
        "alarm": "mdi:alert-circle-outline",
        "alarms": "mdi:alert-circle-outline",
        "fault": "mdi:alert-circle-outline",
        "status": "mdi:information-outline",
        "state": "mdi:information-outline",
        "result": "mdi:information-outline",
        "source": "mdi:information-outline",
        "_default": "mdi:gauge",
    },
    "ups_snmp": {
        "temperature": "mdi:thermometer",
        "temp": "mdi:thermometer",
        "humidity": "mdi:water-percent",
        "frequency": "mdi:sine-wave",
        "voltage": "mdi:sine-wave",
        "volt": "mdi:sine-wave",
        "transfer_point": "mdi:sine-wave",
        "current": "mdi:current-ac",
        "amp": "mdi:current-ac",
        "amps": "mdi:current-ac",
        "amperage": "mdi:current-ac",
        "active_power": "mdi:flash",
        "apparent_power": "mdi:flash-outline",
        "reactive_power": "mdi:flash",
        "real_power": "mdi:flash",
        "power": "mdi:flash",
        "power_factor": "mdi:angle-acute",
        "energy": "mdi:meter-electric",
        "runtime": "mdi:timer-outline",
        "runtime_low": "mdi:timer-alert",
        "seconds_on_battery": "mdi:timer-outline",
        "delay": "mdi:timer-outline",
        "duration": "mdi:timer-outline",
        "load": "mdi:gauge",
        "battery_capacity": "mdi:battery",
        "battery_charge": "mdi:battery",
        "battery_voltage": "mdi:battery",
        "state_of_charge": "mdi:battery",
        "soc": "mdi:battery",
        "battery": "mdi:battery-medium",
        "buzzer_muted": "mdi:volume-off",
        "input_fail": "mdi:alert-circle",
        "bypass_fail": "mdi:alert-circle",
        "general_error": "mdi:alert-circle",
        "inverter_off": "mdi:power",
        "load_on_source": "mdi:power-plug",
        "no_output": "mdi:power-plug-off",
        "output_off": "mdi:power-plug-off",
        "output_disabled": "mdi:power-plug-off",
        "output_shorted": "mdi:flash-alert",
        "overload": "mdi:car-brake-alert",
        "bypass_overload": "mdi:car-brake-alert",
        "bypass": "mdi:transit-detour",
        "line_count": "mdi:transmission-tower",
        "phase_count": "mdi:transmission-tower",
        "alarm": "mdi:alert-circle-outline",
        "alarms": "mdi:alert-circle-outline",
        "fault": "mdi:alert-circle-outline",
        "status": "mdi:information-outline",
        "state": "mdi:information-outline",
        "result": "mdi:information-outline",
        "source": "mdi:information-outline",
        "_default": "mdi:gauge",
    },
    "cyberpower_modbus": {
        "temperature": "mdi:thermometer",
        "temp": "mdi:thermometer",
        "humidity": "mdi:water-percent",
        "frequency": "mdi:sine-wave",
        "voltage": "mdi:sine-wave",
        "volt": "mdi:sine-wave",
        "transfer_point": "mdi:sine-wave",
        "current": "mdi:current-ac",
        "amp": "mdi:current-ac",
        "amps": "mdi:current-ac",
        "amperage": "mdi:current-ac",
        "active_power": "mdi:flash",
        "apparent_power": "mdi:flash-outline",
        "reactive_power": "mdi:flash",
        "real_power": "mdi:flash",
        "power": "mdi:flash",
        "power_factor": "mdi:angle-acute",
        "energy": "mdi:meter-electric",
        "runtime": "mdi:timer-outline",
        "runtime_low": "mdi:timer-alert",
        "seconds_on_battery": "mdi:timer-outline",
        "delay": "mdi:timer-outline",
        "duration": "mdi:timer-outline",
        "load": "mdi:gauge",
        "battery_capacity": "mdi:battery",
        "battery_charge": "mdi:battery",
        "battery_voltage": "mdi:battery",
        "state_of_charge": "mdi:battery",
        "soc": "mdi:battery",
        "battery": "mdi:battery-medium",
        "buzzer_muted": "mdi:volume-off",
        "input_fail": "mdi:alert-circle",
        "bypass_fail": "mdi:alert-circle",
        "general_error": "mdi:alert-circle",
        "inverter_off": "mdi:power",
        "load_on_source": "mdi:power-plug",
        "no_output": "mdi:power-plug-off",
        "output_off": "mdi:power-plug-off",
        "output_disabled": "mdi:power-plug-off",
        "output_shorted": "mdi:flash-alert",
        "overload": "mdi:car-brake-alert",
        "bypass_overload": "mdi:car-brake-alert",
        "bypass": "mdi:transit-detour",
        "line_count": "mdi:transmission-tower",
        "phase_count": "mdi:transmission-tower",
        "alarm": "mdi:alert-circle-outline",
        "alarms": "mdi:alert-circle-outline",
        "fault": "mdi:alert-circle-outline",
        "status": "mdi:information-outline",
        "state": "mdi:information-outline",
        "result": "mdi:information-outline",
        "source": "mdi:information-outline",
        "_default": "mdi:gauge",
    },
    "nut": {
        "battery": "mdi:battery",
        "runtime": "mdi:timer-outline",
        "input_voltage": "mdi:sine-wave",
        "output_voltage": "mdi:sine-wave",
        "load": "mdi:gauge",
        "status": "mdi:information-outline",
        "_default": "mdi:gauge",
    },
}

# Map device source prefixes to (app_name, module_path, resolver_function_name)
SOURCE_TO_APP_MAPPING: dict[str, tuple[str, str, str]] = {
    "apc_modbus": (
        "apc-modbus-ha",
        "custom_components/apc_modbus/icons_unified.py",
        "resolve_sensor_icon",
    ),
    "cyberpower_modbus": (
        "cyberpower-modbus-ha",
        "custom_components/cyberpower_modbus/icons_unified.py",
        "resolve_sensor_icon",
    ),
    "ups_snmp": (
        "ups-snmp-ha",
        "custom_components/ups_snmp_ha/icons_unified.py",
        "resolve_sensor_icon",
    ),
    "nut": (
        "",
        "nut/icons_unified.py",
        "resolve_sensor_icon",
    ),
}

AVAILABILITY_MODULE_MAP: dict[str, tuple[str, str]] = {
    "apc_modbus": (
        "apc-modbus-ha",
        "custom_components/apc_modbus/sensor_availability_unified.py",
    ),
    "cyberpower_modbus": (
        "cyberpower-modbus-ha",
        "custom_components/cyberpower_modbus/sensor_availability_unified.py",
    ),
    "ups_snmp": (
        "ups-snmp-ha",
        "custom_components/ups_snmp_ha/sensor_availability_unified.py",
    ),
    "nut": (
        "",
        "nut/sensor_availability_unified.py",
    ),
}

DEVICE_INFO_MODULE_MAP: dict[str, tuple[str, str]] = {
    "apc_modbus": (
        "apc-modbus-ha",
        "custom_components/apc_modbus/device_info_unified.py",
    ),
    "cyberpower_modbus": (
        "cyberpower-modbus-ha",
        "custom_components/cyberpower_modbus/device_info_unified.py",
    ),
    "ups_snmp": (
        "ups-snmp-ha",
        "custom_components/ups_snmp_ha/device_info_unified.py",
    ),
    "nut": (
        "",
        "nut/device_info_unified.py",
    ),
}

LOCAL_ICON_MODULE_MAP: dict[str, Path] = {
    "apc_modbus": LOCAL_CONTRACTS_DIR / "apc_modbus" / "icons_unified.py",
    "cyberpower_modbus": LOCAL_CONTRACTS_DIR / "cyberpower_modbus" / "icons_unified.py",
    "ups_snmp": LOCAL_CONTRACTS_DIR / "ups_snmp" / "icons_unified.py",
    "nut": LOCAL_CONTRACTS_DIR / "nut" / "icons_unified.py",
}

LOCAL_AVAILABILITY_MODULE_MAP: dict[str, Path] = {
    "apc_modbus": LOCAL_CONTRACTS_DIR / "apc_modbus" / "sensor_availability_unified.py",
    "cyberpower_modbus": LOCAL_CONTRACTS_DIR
    / "cyberpower_modbus"
    / "sensor_availability_unified.py",
    "ups_snmp": LOCAL_CONTRACTS_DIR / "ups_snmp" / "sensor_availability_unified.py",
    "nut": LOCAL_CONTRACTS_DIR / "nut" / "sensor_availability_unified.py",
}

LOCAL_DEVICE_INFO_MODULE_MAP: dict[str, Path] = {
    "apc_modbus": LOCAL_CONTRACTS_DIR / "apc_modbus" / "device_info_unified.py",
    "cyberpower_modbus": LOCAL_CONTRACTS_DIR
    / "cyberpower_modbus"
    / "device_info_unified.py",
    "ups_snmp": LOCAL_CONTRACTS_DIR / "ups_snmp" / "device_info_unified.py",
    "nut": LOCAL_CONTRACTS_DIR / "nut" / "device_info_unified.py",
}

_AVAILABILITY_FUNC = "entity_enabled_default"
_DEVICE_INFO_FUNC = "resolve_device_info"
_DEVICE_INFO_KEYS = frozenset(
    {
        "manufacturer",
        "model",
        "sw_version",
        "hw_version",
        "serial_number",
        "configuration_url",
    }
)


def _get_app_module_path(apps_dir: str, app_name: str, module_path: str) -> Path:
    """Construct the full path to an app module."""
    path = Path(module_path)
    if path.is_absolute():
        return path
    if app_name:
        return Path(apps_dir) / app_name / module_path
    return LOCAL_CONTRACTS_DIR / module_path


def _load_module(module_path: Path) -> object | None:
    """Dynamically load a Python module from a file path."""
    if not module_path.exists():
        LOG.debug("Module not found: %s", module_path)
        return None

    spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
    if spec is None or spec.loader is None:
        LOG.debug("Could not load spec for: %s", module_path)
        return None

    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[str(module_path)] = module
        spec.loader.exec_module(module)
        return module
    except Exception as err:  # grain: ignore NAKED_EXCEPT
        LOG.debug("Failed to load module %s: %s", module_path, err)
        return None


def _resolve_icon_from_fallback(source_prefix: str, metric_key: str) -> str | None:
    """Resolve icon using fallback mappings."""
    mappings = FALLBACK_ICONS.get(source_prefix)
    if mappings is None:
        return None

    key_lower = metric_key.lower()

    # Exact match
    if key_lower in mappings:
        return mappings[key_lower]

    # Substring match (check if any mapping key is in the metric key)
    for pattern, icon in mappings.items():
        if pattern != "_default" and pattern in key_lower:
            return icon

    # Default
    return mappings.get("_default")


def _local_module_for_source(
    device_source: str, module_map: dict[str, Path]
) -> Path | None:
    for prefix, module_path in module_map.items():
        if device_source.startswith(prefix):
            return module_path
    return None


def _prefer_local_contracts(device_source: str) -> bool:
    return driver_owns_runtime_metadata(device_source)


def resolve_icon(
    device_source: str, metric_key: str, apps_dir: str = "/data/apps"
) -> str | None:
    """
    Resolve an mdi icon for a metric based on device source and metric key.

    Uses icon mappings from the external HACS app that provides the source.
    Falls back to built-in mappings if the external app module can't be loaded.

    Args:
        device_source: The device source name (e.g. "apc_modbus_smart")
        metric_key: The metric/register key (e.g. "battery_capacity")
        apps_dir: Path to apps directory (default /data/apps for Docker)

    Returns:
        Icon string like "mdi:battery" or None if icon not resolved
    """
    # Find which app this source belongs to
    app_config = None
    source_prefix = None
    for prefix, config in SOURCE_TO_APP_MAPPING.items():
        if device_source.startswith(prefix):
            app_config = config
            source_prefix = prefix
            break

    if app_config is None:
        LOG.debug("No icon mapping found for source: %s", device_source)
        return None

    app_name, module_path, resolver_func_name = app_config
    if _prefer_local_contracts(device_source):
        local_module_path = _local_module_for_source(
            device_source, LOCAL_ICON_MODULE_MAP
        )
        if local_module_path is None:
            return _resolve_icon_from_fallback(source_prefix, metric_key)
        full_path = local_module_path
    else:
        # Load the module from the external app
        full_path = _get_app_module_path(apps_dir, app_name, module_path)

    # Check cache first
    cache_key = str(full_path)
    if cache_key not in _module_cache:
        module = _load_module(full_path)
        if module is None:
            local_module_path = _local_module_for_source(
                device_source, LOCAL_ICON_MODULE_MAP
            )
            if local_module_path is not None:
                local_cache_key = str(local_module_path)
                local_module = _module_cache.get(local_cache_key)
                if local_module is None:
                    local_module = _load_module(local_module_path)
                    if local_module is not None:
                        _module_cache[local_cache_key] = local_module
                if local_module is not None:
                    module = local_module
                    cache_key = local_cache_key
                else:
                    LOG.debug(
                        "Using fallback icons for %s (could not load local contract %s)",
                        device_source,
                        local_module_path,
                    )
                    return _resolve_icon_from_fallback(source_prefix, metric_key)
            else:
                # If external module fails, use fallback
                LOG.debug(
                    "Using fallback icons for %s (could not load %s)",
                    device_source,
                    app_name,
                )
                return _resolve_icon_from_fallback(source_prefix, metric_key)
        _module_cache[cache_key] = module
    else:
        module = _module_cache[cache_key]

    # Get the resolver function
    if not hasattr(module, resolver_func_name):
        LOG.debug(
            "Module %s does not have function %s, using fallback",
            app_name,
            resolver_func_name,
        )
        return _resolve_icon_from_fallback(source_prefix, metric_key)

    resolver: Callable = getattr(module, resolver_func_name)

    # Call the resolver
    try:
        icon = resolver(metric_key)
        return icon if icon else None
    except Exception as err:  # grain: ignore NAKED_EXCEPT
        LOG.debug(
            "Error resolving icon for %s.%s: %s, using fallback",
            device_source,
            metric_key,
            err,
        )
        return _resolve_icon_from_fallback(source_prefix, metric_key)


def resolve_enabled_by_default(
    device_source: str, metric_key: str, apps_dir: str = "/data/apps"
) -> bool:
    """Return whether a metric entity should be enabled by default in HA.

    Loads sensor_availability_unified.py from the external HACS app and calls
    entity_enabled_default(metric_key). Defaults to True on any failure.

    Args:
        device_source: The device source name (e.g. "apc_modbus_smart")
        metric_key: The metric/register key (e.g. "battery_capacity")
        apps_dir: Path to apps directory (default /data/apps for Docker)

    Returns:
        True if the entity should be enabled by default, False otherwise.
        Defaults to True when the availability module is unavailable.
    """
    avail_config = None
    for prefix, config in AVAILABILITY_MODULE_MAP.items():
        if device_source.startswith(prefix):
            avail_config = config
            break

    if avail_config is None:
        return True

    app_name, module_path = avail_config
    if _prefer_local_contracts(device_source):
        local_module_path = _local_module_for_source(
            device_source, LOCAL_AVAILABILITY_MODULE_MAP
        )
        if local_module_path is None:
            return True
        full_path = local_module_path
    else:
        full_path = _get_app_module_path(apps_dir, app_name, module_path)
    cache_key = str(full_path)

    if cache_key not in _module_cache:
        LOG.debug("Loading availability module for %s from %s", app_name, full_path)
        module = _load_module(full_path)
        if module is None:
            local_module_path = _local_module_for_source(
                device_source, LOCAL_AVAILABILITY_MODULE_MAP
            )
            if local_module_path is None:
                LOG.debug(
                    "Could not load availability module for %s, defaulting to enabled",
                    app_name,
                )
                return True
            module = _load_module(local_module_path)
            if module is None:
                LOG.debug(
                    "Could not load local availability module for %s, defaulting to enabled",
                    device_source,
                )
                return True
        _module_cache[cache_key] = module
    else:
        module = _module_cache[cache_key]

    if not hasattr(module, _AVAILABILITY_FUNC):
        LOG.debug(
            "Module %s missing %s function, defaulting to enabled",
            app_name,
            _AVAILABILITY_FUNC,
        )
        return True

    try:
        result = bool(getattr(module, _AVAILABILITY_FUNC)(metric_key))
        LOG.debug(
            "Availability for %s.%s: enabled=%s", device_source, metric_key, result
        )
        return result
    except Exception as err:  # grain: ignore NAKED_EXCEPT
        LOG.debug(
            "Error resolving availability for %s.%s: %s", device_source, metric_key, err
        )
        return True


def resolve_enabled_defaults(
    device_source: str,
    metric_keys: list[str],
    apps_dir: str = "/data/apps",
    *,
    authoritative: bool = True,
) -> dict[str, bool]:
    """Resolve default-enabled state for a list of metrics.

    Safety guard: if a contract marks every metric disabled, force-enable all
    so devices remain visible by default in HA.
    """
    defaults = {
        key: resolve_enabled_by_default(device_source, key, apps_dir)
        for key in metric_keys
    }
    if defaults and not any(defaults.values()):
        if not authoritative:
            return {key: True for key in metric_keys}
        if device_source not in _all_disabled_defaults_warned_sources:
            LOG.warning(
                "All metrics disabled by default for %s; forcing defaults enabled",
                device_source,
            )
            _all_disabled_defaults_warned_sources.add(device_source)
        return {key: True for key in metric_keys}
    return defaults


def resolve_device_info(
    device_source: str, values: dict[str, object], apps_dir: str = "/data/apps"
) -> dict[str, str]:
    """Resolve canonical device info fields from external HACS contracts.

    Loads device_info_unified.py from the matching external app and calls
    resolve_device_info(values, source). Returns an empty dict on any failure.
    """
    info_config = None
    for prefix, config in DEVICE_INFO_MODULE_MAP.items():
        if device_source.startswith(prefix):
            info_config = config
            break

    if info_config is None:
        return {}

    app_name, module_path = info_config
    if _prefer_local_contracts(device_source):
        local_module_path = _local_module_for_source(
            device_source, LOCAL_DEVICE_INFO_MODULE_MAP
        )
        if local_module_path is None:
            return {}
        full_path = local_module_path
    else:
        full_path = _get_app_module_path(apps_dir, app_name, module_path)
    cache_key = str(full_path)

    if cache_key not in _module_cache:
        module = _load_module(full_path)
        if module is None:
            local_module_path = _local_module_for_source(
                device_source, LOCAL_DEVICE_INFO_MODULE_MAP
            )
            if local_module_path is None:
                LOG.debug(
                    "Could not load device info module for %s from %s",
                    app_name,
                    full_path,
                )
                return {}
            module = _load_module(local_module_path)
            if module is None:
                LOG.debug(
                    "Could not load local device info module for %s from %s",
                    device_source,
                    local_module_path,
                )
                return {}
        _module_cache[cache_key] = module
    else:
        module = _module_cache[cache_key]

    if not hasattr(module, _DEVICE_INFO_FUNC):
        LOG.debug("Module %s missing %s function", app_name, _DEVICE_INFO_FUNC)
        return {}

    resolver: Callable = getattr(module, _DEVICE_INFO_FUNC)
    try:
        raw = resolver(values, device_source)
    except Exception as err:  # grain: ignore NAKED_EXCEPT
        LOG.debug("Error resolving device info for %s: %s", device_source, err)
        return {}

    if not isinstance(raw, dict):
        LOG.debug("Device info resolver for %s returned non-dict", device_source)
        return {}

    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if key not in _DEVICE_INFO_KEYS:
            continue
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        normalized[key] = text
    return normalized
