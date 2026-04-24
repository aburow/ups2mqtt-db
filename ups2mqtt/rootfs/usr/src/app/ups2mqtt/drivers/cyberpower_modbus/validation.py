# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""Contract validation and enforcement for CyberPower driver catalog.

VALIDATION RESPONSIBILITY:
==========================

Validates that the catalog (per-driver exposure schema) maintains its contract:
1. Every entry has a documented source (no invented registers/OIDs)
2. Every entry has tier designation (normalized vs extended)
3. Source locators are valid (register numbers, OID strings)
4. No duplicate canonical keys
5. Aliases do not replace canonical identity

This enforces the architectural boundary: catalog declares exposable datapoints
with truthful source declarations. Validation prevents drift where fields are
added without documented evidence or where raw/register keys are confused with
canonical keys.

See catalog.py module docstring for full architectural model.
"""

from __future__ import annotations

from typing import Any


class CatalogValidationError(Exception):
    """Raised when catalog violates contract rules."""

    pass


def validate_catalog(catalog: dict[str, Any]) -> list[str]:
    """Validate catalog contract compliance.

    Enforces:
    - Every sensor has a documented source (no invented fields)
    - Every sensor has a tier designation
    - Modbus sensors have register numbers
    - SNMP sensors have OIDs
    - No duplicate keys

    Args:
        catalog: Driver catalog dict

    Returns:
        List of validation errors (empty if valid)
    """
    errors = []
    sensors = catalog.get("sensors", [])

    if not sensors:
        errors.append("Catalog has no sensors")
        return errors

    seen_keys = set()

    for idx, sensor in enumerate(sensors):
        sensor_id = f"sensor[{idx}]"

        # Required fields
        key = sensor.get("key")
        if not key:
            errors.append(f"{sensor_id}: missing 'key'")
            continue

        sensor_id = f"sensor[{key}]"

        # Check for duplicates
        if key in seen_keys:
            errors.append(f"{sensor_id}: duplicate key")
        seen_keys.add(key)

        # Tier validation
        tier = sensor.get("tier")
        if not tier:
            errors.append(f"{sensor_id}: missing 'tier'")
        elif tier not in {"normalized", "extended"}:
            errors.append(
                f"{sensor_id}: invalid tier '{tier}' (must be normalized or extended)"
            )

        # Source validation
        source = sensor.get("source")
        if not source:
            errors.append(f"{sensor_id}: missing 'source'")
            continue

        if source not in {"modbus", "snmp", "metadata"}:
            errors.append(f"{sensor_id}: invalid source '{source}'")
            continue

        # Source-specific validation
        if source == "modbus":
            if "register" not in sensor:
                errors.append(f"{sensor_id}: Modbus source missing 'register'")
        elif source == "snmp":
            if "oid" not in sensor:
                errors.append(f"{sensor_id}: SNMP source missing 'oid'")
            else:
                oid = sensor.get("oid", "")
                if not oid or not oid.startswith("1.3.6.1"):
                    errors.append(f"{sensor_id}: invalid OID '{oid}'")
        elif source == "metadata":
            # metadata source is deprecated, should be SNMP
            errors.append(
                f"{sensor_id}: 'metadata' source is deprecated, use 'snmp' with OID"
            )

    # Validate tier model
    tier_model = catalog.get("tier_model")
    if not tier_model:
        errors.append("Catalog missing 'tier_model'")
    else:
        if "normalized" not in tier_model:
            errors.append("tier_model missing 'normalized' tier definition")
        if "extended" not in tier_model:
            errors.append("tier_model missing 'extended' tier definition")

    return errors


def validate_sensor_against_profile(
    sensor: dict[str, Any],
    profile: dict[str, Any],
) -> list[str]:
    """Validate sensor is resolvable from profile.

    Args:
        sensor: Sensor definition from catalog
        profile: Capability profile with registers

    Returns:
        List of validation errors
    """
    errors = []
    source = sensor.get("source")
    key = sensor.get("key")

    if source == "modbus":
        register_addr = sensor.get("register")
        if register_addr is None:
            return errors  # Already caught by catalog validation

        # Check if register exists in profile
        registers = profile.get("registers", [])
        found = False
        for reg in registers:
            if isinstance(reg, dict) and reg.get("address") == register_addr:
                found = True
                break

        if not found:
            errors.append(
                f"Sensor '{key}' references register {register_addr} "
                f"not found in profile"
            )

    return errors


def validate_no_invented_sources(catalog: dict[str, Any]) -> list[str]:
    """Ensure no fields claim unsupported sources.

    This is the core "no invention" guardrail.

    Args:
        catalog: Driver catalog

    Returns:
        List of errors for invented sources
    """
    errors = []
    sensors = catalog.get("sensors", [])

    known_modbus_registers = {
        8192,
        8200,
        8201,
        8206,
        8209,
        8210,
        8212,
        8224,
        8225,
        8226,
        8860,
        12288,
        12289,
        12320,
        12327,
        12418,
        12419,
        12435,
        12436,
    }

    # Known documented SNMP OIDs (from legacy evidence)
    known_snmp_oids = {
        "1.3.6.1.4.1.3808.1.1.1.1.1.1.0",  # model
        "1.3.6.1.4.1.3808.1.1.1.1.1.2.0",  # card_model
        "1.3.6.1.4.1.3808.1.1.1.1.2.1.0",  # ups_firmware
        "1.3.6.1.4.1.3808.1.1.1.1.2.3.0",  # serial
        "1.3.6.1.4.1.3808.1.1.1.1.2.4.0",  # card_firmware (sw_version)
        "1.3.6.1.4.1.3808.1.1.1.2.1.3.0",  # battery_replace_date
    }

    for sensor in sensors:
        key = sensor.get("key")
        source = sensor.get("source")

        if source == "modbus":
            register = sensor.get("register")
            if register not in known_modbus_registers:
                errors.append(
                    f"Sensor '{key}' claims undocumented Modbus register {register}"
                )
        elif source == "snmp":
            oid = sensor.get("oid", "")
            if oid not in known_snmp_oids:
                errors.append(f"Sensor '{key}' claims undocumented SNMP OID '{oid}'")

    return errors
