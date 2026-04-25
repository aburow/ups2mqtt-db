# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""APC Modbus per-driver catalog.

ARCHITECTURAL MODEL:
====================

The catalog is a PER-DRIVER exposure schema that defines ALL exposable datapoints
for this driver family. It is NOT a global schema or runtime state.

Responsibilities:
-----------------
1. CATALOG: Declares all possible datapoints this driver can expose
   - Canonical keys (stable identity)
   - Source declarations (modbus register)
   - Tier assignment (normalized vs extended)
   - Metadata (labels, units, categories)

2. PROFILE: Selects which catalog entries apply to a specific model/variant
   - Profile is a SUBSET/FILTER over catalog applicability
   - Profiles reference catalog entries by canonical key
   - Profiles add polling metadata (poll_group, word_order, etc.)

3. RUNTIME: Resolves selected catalog entries from declared sources
   - Runtime polls only what profile selected from catalog
   - Runtime uses catalog source declarations (register addresses)
   - Runtime merges via canonical keys

4. DISCOVERY: Publishes according to tier and configuration
   - Normalized tier: enabled by default
   - Extended tier: opt-in only (enable_extended_fields)

KEY PRINCIPLES:
---------------
- Catalog uses canonical keys
- Catalog defines exposure capability; profile defines applicability
- Runtime resolves from declared sources; discovery gates by tier

Source: Legacy apps/apc-modbus-ha/custom_components/apc_modbus/registers_smt_ups.py
"""

from __future__ import annotations

from typing import Any

APC_SMT_CATALOG: dict[str, Any] = {
    "profile_id": "apc_modbus_smt",
    "protocol": "multi_source",
    "tier_model": {
        "normalized": {
            "description": "Stable canonical fields, enabled by default for MQTT/HA",
            "enabled_by_default": True,
        },
        "extended": {
            "description": "Additional documented fields, opt-in only",
            "enabled_by_default": False,
        },
    },
    "sensors": [
        # Core battery measurements (normalized - exposed in legacy SENSOR_DESCRIPTIONS)
        {
            "key": "runtime_remaining",
            "label": "Runtime Remaining",
            "source": "modbus",
            "register": 0x0080,
            "unit": "min",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "battery_state_of_charge",
            "label": "Battery State of Charge",
            "source": "modbus",
            "register": 0x0082,
            "unit": "%",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "battery_voltage",
            "label": "Battery Voltage",
            "source": "modbus",
            "register": 0x0083,
            "unit": "V",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "battery_temperature",
            "label": "Battery Temperature",
            "source": "modbus",
            "register": 0x0087,
            "unit": "°C",
            "category": "core",
            "tier": "normalized",
        },
        # Output measurements Phase 1 (normalized)
        {
            "key": "output_load_percent",
            "label": "Output Load",
            "source": "modbus",
            "register": 0x0088,
            "unit": "%",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "output_current",
            "label": "Output Current",
            "source": "modbus",
            "register": 0x008C,
            "unit": "A",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "output_voltage",
            "label": "Output Voltage",
            "source": "modbus",
            "register": 0x008E,
            "unit": "V",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "output_frequency",
            "label": "Output Frequency",
            "source": "modbus",
            "register": 0x0090,
            "unit": "Hz",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "output_energy",
            "label": "Output Energy",
            "source": "modbus",
            "register": 0x0091,
            "unit": "Wh",
            "category": "core",
            "tier": "normalized",
        },
        # Input measurements (normalized)
        {
            "key": "input_voltage",
            "label": "Input Voltage",
            "source": "modbus",
            "register": 0x0097,
            "unit": "V",
            "category": "core",
            "tier": "normalized",
        },
        # Bypass measurements (normalized)
        {
            "key": "bypass_voltage",
            "label": "Bypass Voltage",
            "source": "modbus",
            "register": 0x0094,
            "unit": "V",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "bypass_frequency",
            "label": "Bypass Frequency",
            "source": "modbus",
            "register": 0x0095,
            "unit": "Hz",
            "category": "core",
            "tier": "normalized",
        },
        # Canonical SNMP status fields (normalized - replace derived bitfield booleans)
        # Source: RFC 1628 (UPS-MIB) - canonical output source state
        {
            "key": "output_source",
            "label": "Output Source",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.4.1.0",
            "category": "diagnostic",
            "tier": "normalized",
            "note": "Replaces ups_online/ups_on_battery/ups_on_bypass/ups_output_off - Enum: 1=other, 2=none, 3=normal, 4=bypass, 5=battery, 6=booster, 7=reducer",
        },
        # Canonical SNMP battery status field (normalized - replace battery bitfield booleans)
        # Source: RFC 1628 (UPS-MIB) - canonical battery status
        {
            "key": "battery_status",
            "label": "Battery Status",
            "source": "snmp",
            "oid": "1.3.6.1.2.1.33.1.2.1.0",
            "category": "diagnostic",
            "tier": "normalized",
            "note": "Replaces ups_low_battery - Enum: 1=unknown, 2=batteryNormal, 3=batteryLow, 4=batteryDepleted",
        },
        # External environmental probe measurements (SNMP UIO)
        {
            "key": "measure_ups_temp_probe1",
            "label": "External Temperature Probe 1",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.25.1.2.1.6.1.1",
            "unit": "°C",
            "category": "extended",
            "tier": "extended",
            "note": "UIO external sensor probe 1 temperature",
        },
        {
            "key": "measure_ups_humidity_probe1",
            "label": "External Humidity Probe 1",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.25.1.2.1.7.1.1",
            "unit": "%",
            "category": "extended",
            "tier": "extended",
            "note": "UIO external sensor probe 1 humidity",
        },
        {
            "key": "measure_ups_temp_probe2",
            "label": "External Temperature Probe 2",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.25.1.2.1.6.2.1",
            "unit": "°C",
            "category": "extended",
            "tier": "extended",
            "note": "UIO external sensor probe 2 temperature",
        },
        {
            "key": "measure_ups_humidity_probe2",
            "label": "External Humidity Probe 2",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.25.1.2.1.7.2.1",
            "unit": "%",
            "category": "extended",
            "tier": "extended",
            "note": "UIO external sensor probe 2 humidity",
        },
        # Extended tier - additional measurements from legacy that weren't exposed as sensors
        {
            "key": "battery_voltage_negative",
            "label": "Battery Voltage (Negative)",
            "source": "modbus",
            "register": 0x0084,
            "unit": "V",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "output_load_percent_l2",
            "label": "Output Load (Phase 2)",
            "source": "modbus",
            "register": 0x0089,
            "unit": "%",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "output_apparent_power_percent",
            "label": "Output Apparent Power",
            "source": "modbus",
            "register": 0x008A,
            "unit": "%",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "output_apparent_power_percent_l2",
            "label": "Output Apparent Power (Phase 2)",
            "source": "modbus",
            "register": 0x008B,
            "unit": "%",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "output_current_l2",
            "label": "Output Current (Phase 2)",
            "source": "modbus",
            "register": 0x008D,
            "unit": "A",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "output_voltage_l2",
            "label": "Output Voltage (Phase 2)",
            "source": "modbus",
            "register": 0x008F,
            "unit": "V",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "input_voltage_l2",
            "label": "Input Voltage (Phase 2)",
            "source": "modbus",
            "register": 0x0098,
            "unit": "V",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "input_voltage_l3",
            "label": "Input Voltage (Phase 3)",
            "source": "modbus",
            "register": 0x0099,
            "unit": "V",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "battery_replacement_date_days",
            "label": "Battery Replacement Date (Days)",
            "source": "modbus",
            "register": 0x0085,
            "unit": "d",
            "category": "extended",
            "tier": "extended",
        },
        # Extended tier - raw bitfield registers (kept as-is for diagnostic purposes)
        # These are NOT derived fields - they are direct register reads of status words
        {
            "key": "ups_status_bf",
            "label": "UPS Status Bitfield",
            "source": "modbus",
            "register": 0x0000,
            "category": "extended",
            "tier": "extended",
            "note": "Raw status word register - NOT a derived field",
        },
        {
            "key": "general_error_bf",
            "label": "General Error Bitfield",
            "source": "modbus",
            "register": 0x0013,
            "category": "extended",
            "tier": "extended",
            "note": "Raw error register - NOT a derived field",
        },
        {
            "key": "power_system_error_bf",
            "label": "Power System Error Bitfield",
            "source": "modbus",
            "register": 0x0014,
            "category": "extended",
            "tier": "extended",
            "note": "Raw error register - NOT a derived field",
        },
        {
            "key": "battery_system_error_bf",
            "label": "Battery System Error Bitfield",
            "source": "modbus",
            "register": 0x0016,
            "category": "extended",
            "tier": "extended",
            "note": "Raw error register - NOT a derived field",
        },
        {
            "key": "bypass_input_status_bf",
            "label": "Bypass Input Status Bitfield",
            "source": "modbus",
            "register": 0x0093,
            "category": "extended",
            "tier": "extended",
            "note": "Raw status register - NOT a derived field",
        },
        {
            "key": "input_status_bf",
            "label": "Input Status Bitfield",
            "source": "modbus",
            "register": 0x0096,
            "category": "extended",
            "tier": "extended",
            "note": "Raw status register - NOT a derived field",
        },
    ],
}

# Rack PDU Catalog
# Source: Legacy apps/apc-modbus-ha/custom_components/apc_modbus/registers_rack_pdu.py

APC_RACK_PDU_CATALOG: dict[str, Any] = {
    "profile_id": "apc_modbus_rack_pdu",
    "protocol": "modbus",
    "tier_model": {
        "normalized": {
            "description": "Stable canonical fields for device and phase measurements",
            "enabled_by_default": True,
        },
        "extended": {
            "description": "Additional capability fields, opt-in only",
            "enabled_by_default": False,
        },
    },
    "sensors": [
        # Capability fields (extended tier - metadata)
        {
            "key": "num_phases",
            "label": "Number of Phases",
            "source": "modbus",
            "register": 0x009E,
            "unit": "count",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "num_metered_phases",
            "label": "Number of Metered Phases",
            "source": "modbus",
            "register": 0x009F,
            "unit": "count",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "num_banks",
            "label": "Number of Banks",
            "source": "modbus",
            "register": 0x00A0,
            "unit": "count",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "num_outlets",
            "label": "Number of Outlets",
            "source": "modbus",
            "register": 0x00A1,
            "unit": "count",
            "category": "extended",
            "tier": "extended",
        },
        {
            "key": "num_metered_outlets",
            "label": "Number of Metered Outlets",
            "source": "modbus",
            "register": 0x00A2,
            "unit": "count",
            "category": "extended",
            "tier": "extended",
        },
        # Device-level measurements (normalized tier)
        {
            "key": "device_real_power",
            "label": "Real Power",
            "source": "modbus",
            "register": 0x00CF,
            "unit": "W",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "device_apparent_power",
            "label": "Apparent Power",
            "source": "modbus",
            "register": 0x00D0,
            "unit": "VA",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "device_power_factor",
            "label": "Power Factor",
            "source": "modbus",
            "register": 0x00D1,
            "unit": "pf",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "device_energy",
            "label": "Energy",
            "source": "modbus",
            "register": 0x00D2,
            "unit": "kWh",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "device_load_state",
            "label": "Load State",
            "source": "modbus",
            "register": 0x00D4,
            "category": "diagnostic",
            "tier": "normalized",
        },
        # Phase L1 measurements (normalized tier)
        {
            "key": "phase_L1_current",
            "label": "Phase L1 Current",
            "source": "modbus",
            "register": 0x029B,
            "unit": "A",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "phase_L1_voltage",
            "label": "Phase L1 Voltage",
            "source": "modbus",
            "register": 0x029C,
            "unit": "V",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "phase_L1_real_power",
            "label": "Phase L1 Real Power",
            "source": "modbus",
            "register": 0x029D,
            "unit": "W",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "phase_L1_apparent_power",
            "label": "Phase L1 Apparent Power",
            "source": "modbus",
            "register": 0x029E,
            "unit": "VA",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "phase_L1_power_factor",
            "label": "Phase L1 Power Factor",
            "source": "modbus",
            "register": 0x029F,
            "unit": "pf",
            "category": "measurement",
            "tier": "normalized",
        },
        {
            "key": "phase_L1_state",
            "label": "Phase L1 State",
            "source": "modbus",
            "register": 0x02A0,
            "category": "diagnostic",
            "tier": "normalized",
        },
    ],
}

# APC Smart-UPS Catalog (Multi-Source)
# Source: Legacy registers_smart_ups.py + APC PowerNet MIB
# Multi-source driver supporting modbus (primary) + snmp (metadata/fallback)

APC_SMART_CATALOG: dict[str, Any] = {
    "profile_id": "apc_modbus_smart",
    "protocol": "multi_source",
    "tier_model": {
        "normalized": {
            "description": "Core operational metrics, enabled by default",
            "enabled_by_default": True,
        },
        "extended": {
            "description": "Additional diagnostic and metadata fields, opt-in only",
            "enabled_by_default": False,
        },
    },
    "sensors": [
        # Normalized tier - core battery/power metrics (modbus-only for real-time)
        {
            "key": "battery_state_of_charge",
            "label": "Battery State of Charge",
            "source": "modbus",
            "register": 0x0005,
            "unit": "%",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "runtime_remaining",
            "label": "Runtime Remaining",
            "source": "modbus",
            "register": 0x0006,
            "unit": "min",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "battery_voltage",
            "label": "Battery Voltage",
            "source": "modbus",
            "register": 0x0007,
            "unit": "V",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "load_percent",
            "label": "Load",
            "source": "modbus",
            "register": 0x000C,
            "unit": "%",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "input_voltage",
            "label": "Input Voltage",
            "source": "modbus",
            "register": 0x0011,
            "unit": "V",
            "category": "core",
            "tier": "normalized",
        },
        {
            "key": "input_frequency",
            "label": "Input Frequency",
            "source": "modbus",
            "register": 0x0012,
            "unit": "Hz",
            "category": "core",
            "tier": "normalized",
        },
        # Extended tier - diagnostic fields (modbus)
        {
            "key": "ups_internal_temperature",
            "label": "UPS Internal Temperature",
            "source": "modbus",
            "register": 0x0008,
            "unit": "°C",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "load_amps",
            "label": "Load Current",
            "source": "modbus",
            "register": 0x0009,
            "unit": "A",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "actual_output_voltage",
            "label": "Output Voltage",
            "source": "modbus",
            "register": 0x000E,
            "unit": "V",
            "category": "diagnostic",
            "tier": "extended",
        },
        # Extended tier - metadata (snmp-only for static info)
        {
            "key": "model",
            "label": "Model",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.1.1.1.0",
            "category": "metadata",
            "tier": "extended",
        },
        {
            "key": "serial_number",
            "label": "Serial Number",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.1.2.3.0",
            "category": "metadata",
            "tier": "extended",
        },
        {
            "key": "firmware_version",
            "label": "Firmware Version",
            "source": "snmp",
            "oid": "1.3.6.1.4.1.318.1.1.1.1.2.1.0",
            "category": "metadata",
            "tier": "extended",
        },
        # Extended tier - power quality diagnostics (modbus)
        # Validated on: SMART-UPS 700 + Smart-UPS RT 2000 RM XL
        {
            "key": "max_input_voltage",
            "label": "Max Input Voltage",
            "source": "modbus",
            "register": 0x000F,
            "unit": "V",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "min_input_voltage",
            "label": "Min Input Voltage",
            "source": "modbus",
            "register": 0x0010,
            "unit": "V",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "line_quality",
            "label": "Line Quality",
            "source": "modbus",
            "register": 0x0004,
            "unit": "",
            "category": "diagnostic",
            "tier": "extended",
        },
        # Extended tier - configuration parameters (modbus)
        # Validated on: SMART-UPS 700 + Smart-UPS RT 2000 RM XL
        {
            "key": "nominal_output_voltage",
            "label": "Nominal Output Voltage",
            "source": "modbus",
            "register": 0x000D,
            "unit": "V",
            "category": "configuration",
            "tier": "extended",
        },
        {
            "key": "lower_transfer_point",
            "label": "Lower Transfer Point",
            "source": "modbus",
            "register": 0x001B,
            "unit": "V",
            "category": "configuration",
            "tier": "extended",
        },
        {
            "key": "upper_transfer_point",
            "label": "Upper Transfer Point",
            "source": "modbus",
            "register": 0x001C,
            "unit": "V",
            "category": "configuration",
            "tier": "extended",
        },
        {
            "key": "shutdown_delay",
            "label": "Shutdown Delay",
            "source": "modbus",
            "register": 0x001E,
            "unit": "s",
            "category": "configuration",
            "tier": "extended",
        },
        {
            "key": "low_battery_duration",
            "label": "Low Battery Duration",
            "source": "modbus",
            "register": 0x001F,
            "unit": "min",
            "category": "configuration",
            "tier": "extended",
        },
        {
            "key": "turn_on_delay",
            "label": "Turn On Delay",
            "source": "modbus",
            "register": 0x0020,
            "unit": "s",
            "category": "configuration",
            "tier": "extended",
        },
        # Extended tier - battery health diagnostics (modbus)
        # Validated on: SMART-UPS 700 + Smart-UPS RT 2000 RM XL
        {
            "key": "bad_battery_packs",
            "label": "Bad Battery Packs",
            "source": "modbus",
            "register": 0x000A,
            "unit": "count",
            "category": "diagnostic",
            "tier": "extended",
        },
        {
            "key": "total_battery_packs",
            "label": "Total Battery Packs",
            "source": "modbus",
            "register": 0x000B,
            "unit": "count",
            "category": "diagnostic",
            "tier": "extended",
            "note": "Value 0 = internal battery only, >0 = external battery cabinets",
        },
    ],
}
