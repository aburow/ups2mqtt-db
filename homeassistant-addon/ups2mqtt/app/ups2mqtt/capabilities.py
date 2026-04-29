# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import os
from typing import Any

from .capability_repository import get_capability_repository
from .drivers.runtime_metadata import validate_driver_metadata_ownership
from .icon_resolver import resolve_enabled_defaults

_DEFAULT_SLOW_INTERVAL = 60


def _sanitize_poll_groups(raw: Any) -> dict[str, dict[str, int]]:
    groups: dict[str, dict[str, int]] = {}
    if isinstance(raw, dict):
        for group_name, spec in raw.items():
            if not isinstance(group_name, str) or not group_name.strip():
                continue
            if not isinstance(spec, dict):
                continue
            interval = spec.get("interval_s")
            try:
                interval_int = int(interval)
            except (TypeError, ValueError):
                continue
            if interval_int <= 0:
                continue
            groups[group_name] = {"interval_s": interval_int}
    if "slow" not in groups:
        groups["slow"] = {"interval_s": _DEFAULT_SLOW_INTERVAL}
    return groups


def _collect_metric_keys(profile: dict[str, Any]) -> set[str]:
    protocol = profile.get("protocol")
    if protocol == "modbus":
        return {str(item["key"]) for item in profile.get("registers", [])}
    if protocol == "snmp":
        return set(profile.get("oids", {}).keys())
    if protocol == "hybrid":
        mod_keys = {
            str(item["key"])
            for item in profile.get("modbus", {}).get("registers", [])
            if isinstance(item, dict) and "key" in item
        }
        snmp_keys = set(profile.get("snmp", {}).get("oids", {}).keys())
        return mod_keys | snmp_keys
    if protocol == "nut":
        nut = profile.get("nut", {})
        if not isinstance(nut, dict):
            return set()
        var_keys = {
            str(spec.get("key"))
            for spec in nut.get("variables", {}).values()
            if isinstance(spec, dict) and isinstance(spec.get("key"), str)
        }
        status_keys = {
            str(spec.get("key"))
            for spec in nut.get("status_map", {}).values()
            if isinstance(spec, dict) and isinstance(spec.get("key"), str)
        }
        return var_keys | status_keys
    if protocol == "multi_source":
        keys: set[str] = set()
        active_sources = profile.get("active_sources", {})
        if isinstance(active_sources, dict):
            modbus = active_sources.get("modbus", {})
            if isinstance(modbus, dict):
                for item in modbus.get("registers", []):
                    if isinstance(item, dict) and "key" in item:
                        keys.add(str(item["key"]))
            snmp = active_sources.get("snmp", {})
            if isinstance(snmp, dict):
                oids = snmp.get("oids", {})
                if isinstance(oids, dict):
                    keys.update(str(key) for key in oids.keys())
        return keys
    return set()


def _validate_default_enabled_units(
    *,
    profiles: dict[str, dict[str, Any]],
    repo: Any,
    apps_dir: str,
) -> list[str]:
    """Validate that default-enabled numeric sensors declare units.

    Rule:
    - For default-enabled keys in categories `core` or `measurement`, unit must be set.
    - Status/metadata/diagnostic fields may remain unitless.
    """
    status_suffixes = (
        "_status",
        "_source",
        "_state",
        "_result",
        "_fail",
        "_fault",
        "_present",
        "_active",
        "_muted",
        "_off",
        "_on",
        "_eod",
        "_untrack",
        "_timeout",
        "_shorted",
        "_connected",
        "_low",
        "_bf",
    )

    errors: list[str] = []
    for driver_key, profile in profiles.items():
        metric_keys = sorted(_collect_metric_keys(profile))
        if not metric_keys:
            continue
        defaults = resolve_enabled_defaults(
            driver_key,
            metric_keys,
            apps_dir=apps_dir,
            authoritative=False,
        )
        rows = repo.load_catalog_sensor_rows(driver_key)
        row_by_key = {str(row.get("key", "")): row for row in rows}
        for key in metric_keys:
            if not bool(defaults.get(key, True)):
                continue
            row = row_by_key.get(key)
            if row is None:
                continue
            if str(row.get("unit", "")).strip():
                continue
            category = str(row.get("category", "")).strip().lower()
            if category not in {"core", "measurement"}:
                continue
            key_lower = key.lower()
            if key_lower.endswith(status_suffixes):
                continue
            errors.append(
                f"{driver_key}.{key}: default-enabled {category} sensor missing unit"
            )
    return errors


def load_capabilities(
    path: str = "/usr/src/app/capabilities/capabilities.json",
) -> dict[str, Any]:
    """Load capability metadata from SQLite (canonical source)."""
    del path  # DB is now the canonical source of capability metadata.
    validate_driver_metadata_ownership()

    repo = get_capability_repository()
    repo.seed_baseline_if_needed()
    profiles, runtime_errors = repo.load_runtime_profiles()
    metric_contracts = repo.load_metric_contracts()
    apps_dir = os.environ.get("UPS2MQTT_APPS_DIR", "/data/apps")

    if not profiles:
        raise ValueError("No profiles found in capability database")

    payload: dict[str, Any] = {
        "source": "database",
        "profiles": profiles,
    }
    if metric_contracts:
        payload["metric_contracts"] = metric_contracts
    validation_errors = list(runtime_errors)
    validation_errors.extend(
        _validate_default_enabled_units(
            profiles=profiles,
            repo=repo,
            apps_dir=apps_dir,
        )
    )
    if validation_errors:
        payload["validation_errors"] = validation_errors
    return payload


def source_keys(profile: dict[str, Any]) -> list[str]:
    protocol = profile.get("protocol")
    if protocol == "modbus":
        return [
            str(item["key"])
            for item in profile.get("registers", [])
            if isinstance(item, dict) and "key" in item
        ]
    if protocol == "snmp":
        return [str(key) for key in profile.get("oids", {}).keys()]
    if protocol == "hybrid":
        modbus_keys = [
            str(item["key"])
            for item in profile.get("modbus", {}).get("registers", [])
            if isinstance(item, dict) and "key" in item
        ]
        snmp_keys = [
            str(key)
            for key in profile.get("snmp", {}).get("oids", {}).keys()
            if key not in set(modbus_keys)
        ]
        return modbus_keys + snmp_keys
    if protocol == "multi_source":
        # Multi-source drivers use active_sources structure
        # Extract keys from all transports (modbus + snmp)
        keys: list[str] = []
        active_sources = profile.get("active_sources", {})

        # Get modbus register keys
        modbus_config = active_sources.get("modbus", {})
        if isinstance(modbus_config, dict):
            registers = modbus_config.get("registers", [])
            for item in registers:
                if isinstance(item, dict) and "key" in item:
                    keys.append(str(item["key"]))

        # Get snmp oid keys (exclude duplicates)
        snmp_config = active_sources.get("snmp", {})
        if isinstance(snmp_config, dict):
            oids = snmp_config.get("oids", {})
            if isinstance(oids, dict):
                existing = set(keys)
                for key in oids.keys():
                    if key not in existing:
                        keys.append(str(key))

        return keys
    if protocol == "nut":
        keys: list[str] = []
        nut = profile.get("nut", {})
        if not isinstance(nut, dict):
            return keys
        variables = nut.get("variables", {})
        if isinstance(variables, dict):
            for spec in variables.values():
                if isinstance(spec, dict) and isinstance(spec.get("key"), str):
                    keys.append(str(spec["key"]))
        status_map = nut.get("status_map", {})
        if isinstance(status_map, dict):
            for spec in status_map.values():
                if isinstance(spec, dict) and isinstance(spec.get("key"), str):
                    key = str(spec["key"])
                    if key not in set(keys):
                        keys.append(key)
        return keys
    return []


def bundled_source_keys(
    source: str, path: str = "/usr/src/app/capabilities/capabilities.json"
) -> list[str]:
    """Return metric keys for a source from DB-backed capabilities."""
    del path
    repo = get_capability_repository()
    profiles, _errors = repo.load_runtime_profiles()
    profile = profiles.get(source)
    if not isinstance(profile, dict):
        return []
    return source_keys(profile)


def poll_group_intervals(
    profile: dict[str, Any], default_interval: int
) -> dict[str, int]:
    """Resolve poll group intervals for a profile."""
    fallback = max(1, int(default_interval))
    raw = profile.get("poll_groups", {})
    groups = _sanitize_poll_groups(raw)
    out: dict[str, int] = {}
    for name, spec in groups.items():
        interval = int(spec.get("interval_s", fallback))
        out[name] = max(1, interval)
    if "slow" not in out:
        out["slow"] = fallback
    return out
