# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""Runtime field resolution for CyberPower Modbus driver.

RESOLVER RESPONSIBILITY:
========================

The resolver operates on the catalog (per-driver exposure schema) to:
1. Filter catalog entries by tier (normalized vs extended)
2. Split entries by source type (modbus vs snmp)
3. Build source locator maps (register → canonical_key, OID → canonical_key)
4. Resolve field values via canonical keys and aliases

This is NOT discovery or publication logic. The resolver prepares the runtime
to poll only the entries that:
- Were selected by the profile (applicability to this model)
- Pass tier gating (normalized default, extended opt-in)

The runtime then uses source declarations from the catalog to fetch actual values.

TIER GATING:
============
- normalized: Stable fields, enabled by default (tier.enabled_by_default = True)
- extended: Additional fields, opt-in only (require enable_extended_fields = True)

Discovery/publication happens AFTER runtime resolution, based on the same tier
rules plus user configuration (mqtt_enabled, ha_visible, etc.).

See catalog.py module docstring for full architectural model.
"""

from __future__ import annotations

from typing import Any


def get_enabled_sensors(
    catalog: dict[str, Any],
    enable_extended: bool = False,
) -> list[dict[str, Any]]:
    """Get list of sensors to poll based on tier configuration.

    Args:
        catalog: Driver catalog with tier_model and sensors
        enable_extended: If True, include extended tier fields

    Returns:
        List of sensor definitions to poll
    """
    sensors = catalog.get("sensors", [])
    if not sensors:
        return []

    tier_model = catalog.get("tier_model", {})
    normalized_default = tier_model.get("normalized", {}).get(
        "enabled_by_default", True
    )
    extended_default = tier_model.get("extended", {}).get("enabled_by_default", False)

    enabled_sensors = []
    for sensor in sensors:
        tier = sensor.get("tier", "normalized")

        # Determine if sensor should be included
        should_include = False
        if tier == "normalized" and normalized_default:
            should_include = True
        elif tier == "extended" and (extended_default or enable_extended):
            should_include = True

        if should_include:
            enabled_sensors.append(sensor)

    return enabled_sensors


def get_sensors_by_source(
    sensors: list[dict[str, Any]],
    source: str,
) -> list[dict[str, Any]]:
    """Filter sensors by source type.

    Args:
        sensors: List of sensor definitions
        source: Source type ("modbus", "snmp", etc.)

    Returns:
        Filtered list of sensors matching the source
    """
    return [s for s in sensors if s.get("source") == source]


def get_snmp_oid_map(sensors: list[dict[str, Any]]) -> dict[str, str]:
    """Build mapping of canonical keys to SNMP OIDs.

    Args:
        sensors: List of sensor definitions with source="snmp"

    Returns:
        Dict mapping canonical key to OID string
    """
    oid_map = {}
    for sensor in sensors:
        if sensor.get("source") == "snmp" and "oid" in sensor:
            key = sensor.get("key")
            oid = sensor.get("oid")
            if key and oid:
                oid_map[key] = oid
    return oid_map


def get_modbus_register_keys(
    catalog: dict[str, Any],
    enable_extended: bool = False,
    profile: dict[str, Any] | None = None,
) -> set[str]:
    """Get register keys for Modbus fields enabled by tier configuration.

    Contract: "Canonical-Field-Driven Planning"

    Returns register keys that should be polled based on tier-enabled
    canonical fields in the catalog. Handles mixed canonical/alias namespaces
    by checking what keys the profile actually uses.

    Args:
        catalog: Driver catalog with tier_model and sensors
        enable_extended: If True, include extended tier fields
        profile: Optional profile to determine which namespace (canonical or alias) is used

    Returns:
        Set of register keys to poll
    """
    # Get tier-enabled canonical fields
    enabled_sensors = get_enabled_sensors(catalog, enable_extended)

    # Filter to Modbus source
    modbus_sensors = get_sensors_by_source(enabled_sensors, "modbus")

    # Build profile key set if profile provided
    profile_keys = None
    if profile:
        profile_keys = {reg["key"] for reg in profile.get("registers", [])}

    # Collect register keys
    register_keys = set()
    for sensor in modbus_sensors:
        canonical = sensor["key"]
        aliases = sensor.get("aliases", [])

        if profile_keys:
            # Profile provided - use whichever key (canonical or alias) profile uses
            if canonical in profile_keys:
                register_keys.add(canonical)
            else:
                # Check if any alias is in profile
                for alias in aliases:
                    if alias in profile_keys:
                        register_keys.add(alias)
                        break
        else:
            # No profile - return canonical + all aliases for compatibility
            register_keys.add(canonical)
            register_keys.update(aliases)

    return register_keys


def resolve_field_with_aliases(
    sensor: dict[str, Any],
    modbus_values: dict[str, Any],
) -> tuple[str | None, Any]:
    """Resolve field value from Modbus data considering aliases.

    NAMESPACE SEPARATION:
    - sensor["key"] = canonical key from catalog (e.g., "battery_state_of_charge")
    - sensor["aliases"] = raw register keys (e.g., ["battery_capacity"])
    - modbus_values = keyed by raw register names from registers.py

    This function bridges the raw register namespace (from polling) to the
    canonical catalog namespace (for MQTT/discovery). Canonical key is preferred,
    aliases are fallback for compatibility with register-level keys.

    Args:
        sensor: Sensor definition with key and optional aliases
        modbus_values: Raw Modbus poll results (keyed by register names)

    Returns:
        Tuple of (resolved_key, value) or (None, None) if not found
    """
    # Try canonical key first (catalog namespace)
    canonical_key = sensor.get("key")
    if canonical_key and canonical_key in modbus_values:
        return canonical_key, modbus_values[canonical_key]

    # Try aliases (register namespace compatibility)
    aliases = sensor.get("aliases", [])
    for alias in aliases:
        if alias in modbus_values:
            return alias, modbus_values[alias]

    return None, None
