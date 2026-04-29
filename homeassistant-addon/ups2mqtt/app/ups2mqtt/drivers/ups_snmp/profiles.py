# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""UPS SNMP profiles - selection from catalog for specific MIBs/variants.

PROFILE RESPONSIBILITY:
=======================

A profile is NOT an independent schema. It is a SELECTION/FILTER that specifies:
1. Which catalog entries apply to this specific MIB/variant
2. Polling metadata (poll_group) for runtime optimization
3. OID list for efficient SNMP polling

ARCHITECTURAL FLOW:
===================
catalog.py: All exposable datapoints with canonical keys
    ↓
profiles.py: Selects applicable entries, adds polling metadata
    ↓
resolver.py: Filters by tier (normalized vs extended)
    ↓
pollers.py: Resolves from declared sources (OIDs)
    ↓
discovery: Publishes according to tier and configuration

Source: Legacy apps/ups-snmp-ha/custom_components/ups_snmp_ha/capability_profile_unified.py
"""

from __future__ import annotations

from typing import Any

from .oids import OIDS_APC_MIB, OIDS_UPS_MIB

DEFAULT_POLL_GROUPS: dict[str, dict[str, int]] = {
    "fast": {"interval_s": 10},
    "slow": {"interval_s": 60},
}

# Fast poll OIDs for UPS-MIB - critical runtime metrics
UPS_MIB_FAST_OIDS = {
    "runtime_remaining",
    "battery_charge",
    "output_load",
    "seconds_on_battery",
    "output_source",
    "input_voltage",
}

UPS_MIB_TENTHS_SCALE_OIDS = {
    "battery_voltage",
    "input_frequency",
    "output_frequency",
    "bypass_frequency",
}


def _ups_mib_oid_poll_group(oid_key: str) -> str:
    """Assign fast polling only to frequently changing UPS-MIB status OIDs."""
    return "fast" if oid_key in UPS_MIB_FAST_OIDS else "slow"


def get_ups_mib_profile() -> dict[str, Any]:
    """Get profile for UPS-MIB (RFC 1628) devices.

    Profile = Selection from catalog + polling metadata.
    Selects all fields from OIDS_UPS_MIB and adds polling configuration.

    Source: Legacy capability_profile_unified.py
    """
    # Build OIDs dict with poll_group metadata
    oids: dict[str, dict[str, Any]] = {}
    for key, oid in OIDS_UPS_MIB.items():
        spec: dict[str, Any] = {
            "oid": oid,
            "poll_group": _ups_mib_oid_poll_group(key),
        }
        if key in UPS_MIB_TENTHS_SCALE_OIDS:
            spec["scale"] = 0.1
        oids[key] = spec

    return {
        "profile_id": "ups_snmp_ups_mib",
        "protocol": "snmp",
        "oids": oids,
        "poll_groups": dict(DEFAULT_POLL_GROUPS),
    }


# Fast poll OIDs for APC-MIB - critical runtime metrics
APC_MIB_FAST_OIDS = {
    "runtime_remaining",
    "battery_charge",
    "output_load",
    "output_source",
    "input_voltage",
    "battery_status",
}


def _apc_mib_oid_poll_group(oid_key: str) -> str:
    """Assign fast polling only to frequently changing APC PowerNet OIDs."""
    return "fast" if oid_key in APC_MIB_FAST_OIDS else "slow"


def get_apc_mib_profile() -> dict[str, Any]:
    """Get profile for APC-MIB (PowerNet) devices.

    Profile = Selection from catalog + polling metadata.
    Selects all fields from OIDS_APC_MIB and adds polling configuration.

    Source: Legacy capability_profile_unified.py
    """
    # Build OIDs dict with poll_group metadata
    oids: dict[str, dict[str, Any]] = {}
    for key, oid in OIDS_APC_MIB.items():
        spec: dict[str, Any] = {
            "oid": oid,
            "poll_group": _apc_mib_oid_poll_group(key),
        }
        if key == "runtime_remaining":
            # APC-MIB runtime is TimeTicks (1/100s); normalize to whole minutes.
            spec["timeticks_minutes"] = True
        oids[key] = spec

    return {
        "profile_id": "ups_snmp_apc_mib",
        "protocol": "snmp",
        "oids": oids,
        "poll_groups": dict(DEFAULT_POLL_GROUPS),
    }
