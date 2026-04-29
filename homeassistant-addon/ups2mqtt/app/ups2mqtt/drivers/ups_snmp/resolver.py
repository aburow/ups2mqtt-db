# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""UPS SNMP resolver - tier gating and field resolution.

Responsibilities:
- Filter catalog sensors by tier configuration
- Filter sensors by source (snmp)
- Map canonical keys to OID keys for planning

Pattern: Matches APC Modbus resolver pattern, adapted for SNMP OIDs
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
        source: Source type ("snmp", "modbus", etc.)

    Returns:
        Filtered list of sensors matching the source
    """
    return [s for s in sensors if s.get("source") == source]


def get_snmp_oid_keys(
    catalog: dict[str, Any],
    enable_extended: bool = False,
    profile: dict[str, Any] | None = None,
) -> set[str]:
    """Get OID keys for SNMP fields enabled by tier configuration.

    Contract: "Canonical-Field-Driven Planning"

    Returns OID keys that should be polled based on tier-enabled
    canonical fields in the catalog.

    Args:
        catalog: Driver catalog with tier_model and sensors
        enable_extended: If True, include extended tier fields
        profile: Optional profile (unused - catalog drives planning)

    Returns:
        Set of OID keys to poll
    """
    # Get tier-enabled canonical fields
    enabled_sensors = get_enabled_sensors(catalog, enable_extended)

    # Filter to SNMP source
    snmp_sensors = get_sensors_by_source(enabled_sensors, "snmp")

    # Collect OID keys (canonical keys that map to OIDs)
    oid_keys = {sensor["key"] for sensor in snmp_sensors}

    return oid_keys
