# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""UPS SNMP per-driver catalog.

ARCHITECTURAL MODEL:
====================

The catalog is a PER-DRIVER exposure schema that defines ALL exposable datapoints
for this driver family. It is NOT a global schema or runtime state.

Responsibilities:
-----------------
1. CATALOG: Declares all possible datapoints this driver can expose
   - Canonical keys (stable identity)
   - Source declarations (snmp OID or derived)
   - Tier assignment (normalized vs extended)
   - Metadata (labels, units, categories)

2. PROFILE: Selects which catalog entries apply to a specific MIB/variant
   - Profile is a SUBSET/FILTER over catalog applicability
   - Profiles reference catalog entries by canonical key
   - Profiles add polling metadata (poll_group, etc.)

3. RUNTIME: Resolves selected catalog entries from declared sources
   - Runtime polls only what profile selected from catalog
   - Runtime uses catalog source declarations (OIDs)
   - Runtime merges via canonical keys

4. DISCOVERY: Publishes according to tier and configuration
   - Normalized tier: enabled by default
   - Extended tier: opt-in only (enable_extended_fields)

KEY PRINCIPLES:
---------------
- Catalog uses canonical keys
- Catalog defines exposure capability; profile defines applicability
- Runtime resolves from declared sources; discovery gates by tier

Source: Legacy apps/ups-snmp-ha/custom_components/ups_snmp_ha/sensor_catalog_unified.py
"""

from __future__ import annotations

from typing import Any

# UPS-MIB Catalog
# Source: Legacy sensor_catalog_unified.py ups_snmp_ups_mib section
# Based on: RFC 1628 UPS MIB (1.3.6.1.2.1.33)

UPS_MIB_CATALOG: dict[str, Any] = {
    "profile_id": "ups_snmp_ups_mib",
    "protocol": "snmp",
    "tier_model": {
        "normalized": {
            "description": "Core canonical fields, enabled by default for MQTT/HA",
            "enabled_by_default": True,
        },
        "extended": {
            "description": "Additional diagnostic and metadata fields, opt-in only",
            "enabled_by_default": False,
        },
    },
    "sensors": [
        # Normalized tier (core operational metrics)
        {
            "key": "output_source",
            "label": "Output Source",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.4.1.0",
            "category": "measurement",
            "tier": "normalized",
            "aliases": ["output_source_raw"],
        },
        {
            "key": "runtime_remaining",
            "label": "Runtime Remaining",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.2.3.0",
            "unit": "min",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "output_load",
            "label": "Output Load",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.4.4.1.5.1",
            "unit": "%",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "seconds_on_battery",
            "label": "Seconds On Battery",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.2.2.0",
            "unit": "s",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "battery_charge",
            "label": "Battery Charge",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.2.4.0",
            "unit": "%",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "input_voltage",
            "label": "Input Voltage",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.3.3.1.3.1",
            "unit": "V",
            "category": "measurement",
            "tier": "normalized",
        },
        # Extended tier (diagnostic fields)
        {
            "key": "output_frequency",
            "label": "Output Frequency",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.4.2.0",
            "unit": "Hz",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "output_line_count",
            "label": "Output Line Count",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.4.3.0",
            "unit": "count",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "battery_status",
            "label": "Battery Status",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.2.1.0",
            "category": "diagnostic",
            "tier": "extended",
            "aliases": ["battery_status_text"],
        },
        {
            "key": "battery_temperature",
            "label": "Battery Temperature",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.2.7.0",
            "unit": "°C",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "battery_voltage",
            "label": "Battery Voltage",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.2.5.0",
            "unit": "V",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "alarms_present",
            "label": "Alarms Present",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.6.1.0",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "bypass_frequency",
            "label": "Bypass Frequency",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.5.1.0",
            "unit": "Hz",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "bypass_line_count",
            "label": "Bypass Line Count",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.5.2.0",
            "unit": "count",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "input_frequency",
            "label": "Input Frequency",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.3.3.1.2.1",
            "unit": "Hz",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "input_line_count",
            "label": "Input Line Count",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.3.2.0",
            "unit": "count",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "input_current",
            "label": "Input Current",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.3.3.1.4.1",
            "unit": "A",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "input_power",
            "label": "Input Power",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.3.3.1.5.1",
            "unit": "W",
            "category": "diagnostic",
            "tier": "extended",
        },
        # Extended tier (metadata fields)
        {
            "key": "manufacturer",
            "label": "Manufacturer",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.1.1.0",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "model",
            "label": "Model",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.1.2.0",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "firmware",
            "label": "Firmware",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.1.3.0",
            "category": "extended",
            "tier": "extended",
            "aliases": ["sw_version"],
        },
        {
            "key": "name",
            "label": "Name",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.1.5.0",
            "category": "extended",
            "tier": "extended",
        },
    ],
}

# APC-MIB Catalog
# Source: Legacy sensor_catalog_unified.py ups_snmp_apc_mib section
# Based on: APC PowerNet MIB (1.3.6.1.4.1.318.1.1.1)

APC_MIB_CATALOG: dict[str, Any] = {
    "profile_id": "ups_snmp_apc_mib",
    "protocol": "snmp",
    "tier_model": {
        "normalized": {
            "description": "Core canonical fields, enabled by default for MQTT/HA",
            "enabled_by_default": True,
        },
        "extended": {
            "description": "Additional diagnostic and metadata fields, opt-in only",
            "enabled_by_default": False,
        },
    },
    "sensors": [
        # Normalized tier (core operational metrics)
        {
            "key": "output_source",
            "label": "Output Source",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.4.1.1.0",
            "category": "measurement",
            "tier": "normalized",
            "aliases": ["output_source_raw"],
        },
        {
            "key": "runtime_remaining",
            "label": "Runtime Remaining",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.2.2.3.0",
            "unit": "min",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "output_load",
            "label": "Output Load",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.4.2.3.0",
            "unit": "%",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "battery_charge",
            "label": "Battery Charge",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.2.2.1.0",
            "unit": "%",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "input_voltage",
            "label": "Input Voltage",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.3.2.1.0",
            "unit": "V",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "battery_status",
            "label": "Battery Status",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.2.1.1.0",
            "category": "measurement",
            "tier": "normalized",
            "aliases": ["battery_status_text"],
        },
        # Extended tier (diagnostic fields)
        {
            "key": "battery_temperature",
            "label": "Battery Temperature",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.2.2.2.0",
            "unit": "°C",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "output_voltage",
            "label": "Output Voltage",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.4.2.1.0",
            "unit": "V",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "output_frequency",
            "label": "Output Frequency",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.4.2.2.0",
            "unit": "Hz",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "input_frequency",
            "label": "Input Frequency",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.3.2.4.0",
            "unit": "Hz",
            "category": "diagnostic",
            "tier": "extended",
        },
        # Extended tier (metadata fields)
        {
            "key": "model",
            "label": "Model",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.1.1.1.0",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "location",
            "label": "Location",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.1.1.2.0",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "serial_number",
            "label": "Serial Number",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.1.2.3.0",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "firmware",
            "label": "Firmware",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.1.2.1.0",
            "category": "extended",
            "tier": "extended",
            "aliases": ["sw_version"],
        },
        {
            "key": "firmware_date",
            "label": "Firmware Date",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.1.2.2.0",
            "category": "extended",
            "tier": "extended",
            "aliases": ["hw_version"],
        },
    ],
}
