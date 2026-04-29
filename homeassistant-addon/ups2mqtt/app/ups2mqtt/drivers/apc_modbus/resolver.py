# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""APC Modbus resolver - tier gating and field resolution.

Responsibilities:
- Filter catalog sensors by tier configuration
- Filter sensors by source (modbus)
- Map canonical keys to register keys for planning

Pattern: Matches CyberPower Modbus resolver exactly
"""

from __future__ import annotations

from typing import Any


def get_enabled_sensors(
    catalog: dict[str, Any],
    enable_extended: bool = False,
) -> list[dict[str, Any]]:
    """Get sensors enabled by tier configuration.

    Contract: "Canonical-Field-Driven Planning"

    Args:
        catalog: Driver catalog with tier_model and sensors
        enable_extended: If True, include extended tier fields

    Returns:
        List of enabled sensor definitions
    """
    sensors = catalog.get("sensors", [])
    tier_model = catalog.get("tier_model", {})

    # Determine which tiers are enabled
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


def get_modbus_register_keys(
    catalog: dict[str, Any],
    enable_extended: bool = False,
    profile: dict[str, Any] | None = None,
) -> set[str]:
    """Get register keys for Modbus fields enabled by tier configuration.

    Contract: "Canonical-Field-Driven Planning"

    Returns register keys that should be polled based on tier-enabled
    canonical fields in the catalog. APC SMT uses canonical keys directly
    (no alias mapping needed like CyberPower has).

    Args:
        catalog: Driver catalog with tier_model and sensors
        enable_extended: If True, include extended tier fields
        profile: Optional profile (unused for APC SMT - canonical keys used directly)

    Returns:
        Set of register keys to poll
    """
    # Get tier-enabled canonical fields
    enabled_sensors = get_enabled_sensors(catalog, enable_extended)

    # Filter to Modbus source
    modbus_sensors = get_sensors_by_source(enabled_sensors, "modbus")

    # Collect register keys
    # APC SMT uses canonical keys directly (no aliases)
    register_keys = {sensor["key"] for sensor in modbus_sensors}

    return register_keys


def get_snmp_oid_keys(
    catalog: dict[str, Any],
    enable_extended: bool = False,
) -> set[str]:
    """Get OID keys for SNMP fields enabled by tier configuration.

    Contract: "Canonical-Field-Driven Planning"

    Returns OID keys that should be polled based on tier-enabled
    canonical fields in the catalog.

    Args:
        catalog: Driver catalog with tier_model and sensors
        enable_extended: If True, include extended tier fields

    Returns:
        Set of OID keys to poll
    """
    # Get tier-enabled canonical fields
    enabled_sensors = get_enabled_sensors(catalog, enable_extended)

    # Filter to SNMP source
    snmp_sensors = get_sensors_by_source(enabled_sensors, "snmp")

    # Collect OID keys
    oid_keys = {sensor["key"] for sensor in snmp_sensors}

    return oid_keys
