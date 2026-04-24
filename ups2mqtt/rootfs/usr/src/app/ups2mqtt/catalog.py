# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""Shared catalog sensor loading for runtime and web layers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from .drivers.runtime_metadata import (
    driver_owns_runtime_metadata,
    load_plugin_sensor_catalog,
)

# Driver catalog specifications: driver_key -> (app_dir_name, catalog_path, profile_key)
CATALOG_DRIVER_SPECS: dict[str, tuple[str, str, str]] = {
    "apc_modbus_rack_pdu": (
        "apc-modbus-ha",
        "custom_components/apc_modbus/sensor_catalog_unified.py",
        "apc_modbus_rack_pdu",
    ),
    "apc_modbus_smart": (
        "apc-modbus-ha",
        "custom_components/apc_modbus/sensor_catalog_unified.py",
        "apc_modbus_smart",
    ),
    "apc_modbus_smt": (
        "apc-modbus-ha",
        "custom_components/apc_modbus/sensor_catalog_unified.py",
        "apc_modbus_smt",
    ),
    "ups_snmp_apc_mib": (
        "ups-snmp-ha",
        "custom_components/ups_snmp_ha/sensor_catalog_unified.py",
        "ups_snmp_apc_mib",
    ),
    "ups_snmp_ups_mib": (
        "ups-snmp-ha",
        "custom_components/ups_snmp_ha/sensor_catalog_unified.py",
        "ups_snmp_ups_mib",
    ),
    "cyberpower_modbus_single_phase": (
        "cyberpower-modbus-ha",
        "custom_components/cyberpower_modbus/sensor_catalog_unified.py",
        "cyberpower_modbus_single_phase",
    ),
    "cyberpower_modbus_three_phase": (
        "cyberpower-modbus-ha",
        "custom_components/cyberpower_modbus/sensor_catalog_unified.py",
        "cyberpower_modbus_three_phase",
    ),
}

CATALOG_DRIVER_KEYS = set(CATALOG_DRIVER_SPECS.keys())

# Global cache: apps_dir -> profile_key -> sensor rows
_CATALOG_CACHE: dict[str, dict[str, list[dict[str, str]]]] = {}
_CATALOG_DERIVED_METRICS_CACHE: dict[str, dict[str, list[dict[str, Any]]]] = {}
_PLUGIN_CATALOG_CACHE: dict[str, list[dict[str, str]]] = {}
_PLUGIN_DERIVED_METRICS_CACHE: dict[str, list[dict[str, Any]]] = {}


def _load_catalog_profile_data(
    *,
    driver_key: str,
    apps_dir: str,
) -> tuple[dict[str, Any], str]:
    """Load and validate raw catalog profile data for a driver."""
    spec = CATALOG_DRIVER_SPECS.get(driver_key)
    if spec is None:
        return {}, ""

    app_dir_name, relative_path, profile_key = spec
    catalog_path = Path(apps_dir) / app_dir_name / relative_path
    if not catalog_path.exists():
        raise ValueError(f"Missing sensor catalog for {driver_key}: {catalog_path}")

    try:
        module_spec = importlib.util.spec_from_file_location(
            f"sensor_catalog_{app_dir_name.replace('-', '_')}",
            str(catalog_path),
        )
        module = importlib.util.module_from_spec(module_spec) if module_spec else None
        if not module_spec or not module_spec.loader or module is None:
            raise ValueError(f"Failed to load sensor catalog module: {catalog_path}")
        module_spec.loader.exec_module(module)
        catalog_raw = getattr(module, "ALL_SENSORS_UNIFIED", None)
        if not isinstance(catalog_raw, dict):
            raise ValueError(
                f"Invalid sensor catalog format for {driver_key}: "
                "ALL_SENSORS_UNIFIED must be a dict"
            )
        profile_data = catalog_raw.get(profile_key)
        if not isinstance(profile_data, dict):
            raise ValueError(
                f"Missing catalog profile block '{profile_key}' for {driver_key}"
            )
        return profile_data, profile_key
    except ValueError:
        raise
    except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
        raise ValueError(
            f"Failed to parse sensor catalog for {driver_key}: {err}"
        ) from err


def get_catalog_keys(driver_key: str, apps_dir: str) -> set[str]:
    """
    Get sensor keys from driver catalog.

    Args:
        driver_key: Driver identifier (e.g., "apc_modbus_smart")
        apps_dir: Path to apps directory containing catalog files

    Returns:
        Set of catalog sensor keys for the driver

    Raises:
        ValueError: If catalog is invalid or missing
    """
    spec = CATALOG_DRIVER_SPECS.get(driver_key)
    if spec is None:
        return set()

    if driver_owns_runtime_metadata(driver_key):
        if driver_key not in _PLUGIN_CATALOG_CACHE:
            profile_data = load_plugin_sensor_catalog(driver_key)
            sensors = profile_data.get("sensors")
            if not isinstance(sensors, list):
                raise ValueError(
                    f"Invalid plugin catalog sensors block for {driver_key}: expected list"
                )
            rows: list[dict[str, str]] = []
            seen_keys: set[str] = set()
            for item in sensors:
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Invalid plugin sensor entry for {driver_key}: expected object"
                    )
                key = str(item.get("key", "")).strip()
                if not key:
                    raise ValueError(
                        f"Invalid plugin sensor entry for {driver_key}: missing key"
                    )
                if key in seen_keys:
                    raise ValueError(
                        f"Duplicate plugin catalog key '{key}' for {driver_key}"
                    )
                seen_keys.add(key)
                aliases = item.get("aliases", [])
                alias_list = (
                    [str(alias) for alias in aliases if str(alias)]
                    if isinstance(aliases, list)
                    else []
                )
                reference_value = ""
                if "register" in item:
                    reference_value = str(item.get("register", "")).strip()
                elif "oid" in item:
                    reference_value = str(item.get("oid", "")).strip()
                rows.append(
                    {
                        "key": key,
                        "label": str(item.get("label", key)),
                        "category": str(item.get("category", "other")).strip()
                        or "other",
                        "unit": str(item.get("unit", "")).strip(),
                        "source": str(item.get("source", "")).strip(),
                        "aliases": ", ".join(alias_list),
                        "reference": reference_value,
                    }
                )
            if not rows:
                raise ValueError(f"Empty plugin sensor catalog for {driver_key}")
            _PLUGIN_CATALOG_CACHE[driver_key] = rows

            derived_metrics_raw = profile_data.get("derived_metrics", [])
            if derived_metrics_raw is None:
                derived_metrics_raw = []
            if not isinstance(derived_metrics_raw, list):
                raise ValueError(
                    f"Invalid plugin derived_metrics block for {driver_key}: expected list"
                )
            sanitized_metrics: list[dict[str, Any]] = []
            for item in derived_metrics_raw:
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Invalid plugin derived metric entry for {driver_key}: expected object"
                    )
                sanitized_metrics.append(dict(item))
            _PLUGIN_DERIVED_METRICS_CACHE[driver_key] = sanitized_metrics

        return {str(row["key"]) for row in _PLUGIN_CATALOG_CACHE.get(driver_key, [])}

    _, _, profile_key = spec
    catalog = _CATALOG_CACHE.setdefault(apps_dir, {})
    derived_cache = _CATALOG_DERIVED_METRICS_CACHE.setdefault(apps_dir, {})

    if profile_key not in catalog:
        profile_data, _ = _load_catalog_profile_data(
            driver_key=driver_key, apps_dir=apps_dir
        )
        sensors = profile_data.get("sensors")
        if not isinstance(sensors, list):
            raise ValueError(
                f"Invalid catalog sensors block for {driver_key}: expected list"
            )

        rows: list[dict[str, str]] = []
        seen_keys: set[str] = set()
        for item in sensors:
            if not isinstance(item, dict):
                raise ValueError(
                    f"Invalid sensor entry in catalog for {driver_key}: expected object"
                )
            key = str(item.get("key", "")).strip()
            if not key:
                raise ValueError(
                    f"Invalid sensor entry in catalog for {driver_key}: missing key"
                )
            if key in seen_keys:
                raise ValueError(f"Duplicate catalog key '{key}' for {driver_key}")
            seen_keys.add(key)
            aliases = item.get("aliases", [])
            alias_list = (
                [str(alias) for alias in aliases if str(alias)]
                if isinstance(aliases, list)
                else []
            )
            reference_value = ""
            if "register" in item:
                reference_value = str(item.get("register", "")).strip()
            elif "oid" in item:
                reference_value = str(item.get("oid", "")).strip()
            rows.append(
                {
                    "key": key,
                    "label": str(item.get("label", key)),
                    "category": str(item.get("category", "other")).strip() or "other",
                    "unit": str(item.get("unit", "")).strip(),
                    "source": str(item.get("source", "")).strip(),
                    "aliases": ", ".join(alias_list),
                    "reference": reference_value,
                }
            )
        if not rows:
            raise ValueError(f"Empty sensor catalog for {driver_key}")
        catalog[profile_key] = rows

        derived_metrics_raw = profile_data.get("derived_metrics", [])
        if derived_metrics_raw is None:
            derived_metrics_raw = []
        if not isinstance(derived_metrics_raw, list):
            raise ValueError(
                f"Invalid catalog derived_metrics block for {driver_key}: expected list"
            )
        sanitized_metrics: list[dict[str, Any]] = []
        for item in derived_metrics_raw:
            if not isinstance(item, dict):
                raise ValueError(
                    f"Invalid derived metric entry in catalog for {driver_key}: expected object"
                )
            sanitized_metrics.append(dict(item))
        derived_cache[profile_key] = sanitized_metrics

    if profile_key not in catalog:
        raise ValueError(
            f"Missing catalog profile block '{profile_key}' for {driver_key}"
        )

    # Extract keys from cached rows
    return {str(row["key"]) for row in catalog.get(profile_key, [])}


def get_catalog_derived_metrics(driver_key: str, apps_dir: str) -> list[dict[str, Any]]:
    """Get catalog-declared derived metric definitions for a driver."""
    spec = CATALOG_DRIVER_SPECS.get(driver_key)
    if spec is None:
        return []
    if driver_owns_runtime_metadata(driver_key):
        get_catalog_keys(driver_key, apps_dir)
        metrics = _PLUGIN_DERIVED_METRICS_CACHE.get(driver_key, [])
        return [dict(item) for item in metrics]
    get_catalog_keys(driver_key, apps_dir)
    _, _, profile_key = spec
    derived_cache = _CATALOG_DERIVED_METRICS_CACHE.get(apps_dir, {})
    metrics = derived_cache.get(profile_key, [])
    return [dict(item) for item in metrics]


def get_catalog_sensor_rows(driver_key: str, apps_dir: str) -> list[dict[str, str]]:
    """
    Get full sensor row data from driver catalog.

    This is used by web.py for UI rendering with labels, categories, etc.
    For runtime MQTT filtering, use get_catalog_keys() instead.

    Args:
        driver_key: Driver identifier (e.g., "apc_modbus_smart")
        apps_dir: Path to apps directory containing catalog files

    Returns:
        List of sensor row dicts with keys, labels, categories, etc.

    Raises:
        ValueError: If catalog is invalid or missing
    """
    spec = CATALOG_DRIVER_SPECS.get(driver_key)
    if spec is None:
        return []
    if driver_owns_runtime_metadata(driver_key):
        get_catalog_keys(driver_key, apps_dir)
        return list(_PLUGIN_CATALOG_CACHE.get(driver_key, []))

    # Trigger cache population by calling get_catalog_keys
    get_catalog_keys(driver_key, apps_dir)

    # Return cached rows
    _, _, profile_key = spec
    catalog = _CATALOG_CACHE.get(apps_dir, {})
    return list(catalog.get(profile_key, []))
