# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""APC Modbus profiles - selection from catalog for specific models/variants.

PROFILE RESPONSIBILITY:
=======================

A profile is NOT an independent schema. It is a SELECTION/FILTER that specifies:
1. Which catalog entries apply to this specific UPS model/variant
2. Polling metadata (poll_group, word_order) for runtime optimization
3. Register blocks for efficient batch reads

ARCHITECTURAL FLOW:
===================
catalog.py: All exposable datapoints with canonical keys
    ↓
profiles.py: Selects applicable entries, adds polling metadata
    ↓
resolver.py: Filters by tier (normalized vs extended)
    ↓
pollers.py: Resolves from declared sources (register addresses)
    ↓
discovery: Publishes according to tier and configuration

Source: Legacy apps/apc-modbus-ha/custom_components/apc_modbus/registers_smt_ups.py
"""

from __future__ import annotations

from typing import Any

from .registers import (
    REGISTER_BLOCKS_RACK_PDU,
    REGISTER_BLOCKS_SMT,
    REGISTERS_RACK_PDU,
    REGISTERS_SMT,
)

DEFAULT_POLL_GROUPS: dict[str, dict[str, int]] = {
    "fast": {"interval_s": 10},
    "slow": {"interval_s": 60},
}

# Fast poll keys for SMT - critical runtime metrics
SMT_FAST_KEYS = {
    "runtime_remaining",
    "battery_state_of_charge",
    "battery_voltage",
    "output_load_percent",
    "output_current",
    "output_voltage",
    "input_voltage",
    "ups_on_battery",
    "ups_online",
}


def _smt_register_poll_group(metric_key: str) -> str:
    """Map high-churn SMT metrics to fast polling and everything else to slow."""
    return "fast" if metric_key in SMT_FAST_KEYS else "slow"


def _smt_block_poll_group(block_name: str) -> str:
    """Determine poll group for SMT register block."""
    name = block_name.lower()
    if "measurement" in name:
        return "fast"
    return "slow"


def get_smt_profile() -> dict[str, Any]:
    """Get profile for APC SMT/SMX/SRT UPS devices.

    Multi-source profile with modbus (primary, real-time) + snmp (canonical status).
    This is a bounded multi-transport model: modbus for measurements, SNMP for status.

    Profile = Selection from catalog + polling metadata.
    Selects all fields from REGISTERS_SMT and adds polling configuration.

    Source: Legacy registers_smt_ups.py + RFC 1628 (UPS-MIB)
    """
    # Modbus registers (real-time measurements + raw bitfield registers)
    modbus_registers: list[dict[str, Any]] = []
    for register in REGISTERS_SMT:
        item = dict(register)
        item["poll_group"] = _smt_register_poll_group(str(item["key"]))
        if "word_order" not in item:
            item["word_order"] = "big"
        modbus_registers.append(item)

    # Modbus register blocks for efficient batch reads
    modbus_blocks: list[dict[str, Any]] = []
    for block in REGISTER_BLOCKS_SMT:
        modbus_blocks.append(
            {
                "name": block["name"],
                "start_address": block["start_address"],
                "count": block["count"],
                "poll_group": _smt_block_poll_group(str(block["name"])),
            }
        )

    # SNMP OIDs (canonical status + UIO environmental probes)
    snmp_oids = {
        "output_source": {
            "oid": "1.3.6.1.2.1.33.1.4.1.0",
            "poll_group": "fast",
        },
        "battery_status": {
            "oid": "1.3.6.1.2.1.33.1.2.1.0",
            "poll_group": "fast",
        },
        "measure_ups_temp_probe1": {
            "oid": "1.3.6.1.4.1.318.1.1.25.1.2.1.6.1.1",
            "oids": [
                "1.3.6.1.4.1.318.1.1.25.1.2.1.6.1.1",
                "1.3.6.1.4.1.318.1.1.25.1.2.1.6.1",
            ],
            "poll_group": "slow",
            "parser": "external_temp_c",
        },
        "measure_ups_humidity_probe1": {
            "oid": "1.3.6.1.4.1.318.1.1.25.1.2.1.7.1.1",
            "oids": [
                "1.3.6.1.4.1.318.1.1.25.1.2.1.7.1.1",
                "1.3.6.1.4.1.318.1.1.25.1.2.1.7.1",
            ],
            "poll_group": "slow",
            "parser": "external_humidity_pct",
        },
        "measure_ups_temp_probe2": {
            "oid": "1.3.6.1.4.1.318.1.1.25.1.2.1.6.2.1",
            "oids": [
                "1.3.6.1.4.1.318.1.1.25.1.2.1.6.2.1",
                "1.3.6.1.4.1.318.1.1.25.1.2.1.6.2",
            ],
            "poll_group": "slow",
            "parser": "external_temp_c",
        },
        "measure_ups_humidity_probe2": {
            "oid": "1.3.6.1.4.1.318.1.1.25.1.2.1.7.2.1",
            "oids": [
                "1.3.6.1.4.1.318.1.1.25.1.2.1.7.2.1",
                "1.3.6.1.4.1.318.1.1.25.1.2.1.7.2",
            ],
            "poll_group": "slow",
            "parser": "external_humidity_pct",
        },
    }

    return {
        "profile_id": "apc_modbus_smt",
        "protocol": "multi_source",
        # Bounded multi-transport configuration
        "active_sources": {
            "modbus": {
                "enabled": True,
                "registers": modbus_registers,
                "register_blocks": modbus_blocks,
            },
            "snmp": {
                "enabled": True,
                "oids": snmp_oids,
            },
        },
        "poll_groups": dict(DEFAULT_POLL_GROUPS),
    }


# Fast poll keys for Rack PDU - critical power metrics
RACK_PDU_FAST_KEYS = {
    "device_real_power",
    "device_apparent_power",
    "device_power_factor",
    "phase_L1_current",
    "phase_L1_voltage",
    "phase_L1_real_power",
}


def _rack_pdu_register_poll_group(metric_key: str) -> str:
    """Map selected Rack PDU power metrics to fast polling cadence."""
    return "fast" if metric_key in RACK_PDU_FAST_KEYS else "slow"


def _rack_pdu_block_poll_group(block_name: str) -> str:
    """Use fast cadence for measurement/phase blocks and slow for other blocks."""
    name = block_name.lower()
    if "measurement" in name or "phase" in name:
        return "fast"
    return "slow"


def get_rack_pdu_profile() -> dict[str, Any]:
    """Get profile for APC Rack PDU devices.

    Profile = Selection from catalog + polling metadata.
    Selects all fields from REGISTERS_RACK_PDU and adds polling configuration.

    Source: Legacy registers_rack_pdu.py
    Note: This provides a base profile for single-phase configuration.
    Dynamic outlet/bank registers are handled at runtime based on capabilities.
    """
    registers: list[dict[str, Any]] = []
    for register in REGISTERS_RACK_PDU:
        item = dict(register)
        item["poll_group"] = _rack_pdu_register_poll_group(str(item["key"]))
        if "word_order" not in item:
            item["word_order"] = "big"
        registers.append(item)

    blocks: list[dict[str, Any]] = []
    for block in REGISTER_BLOCKS_RACK_PDU:
        blocks.append(
            {
                "name": block["name"],
                "start_address": block["start_address"],
                "count": block["count"],
                "poll_group": _rack_pdu_block_poll_group(str(block["name"])),
            }
        )

    return {
        "profile_id": "apc_modbus_rack_pdu",
        "protocol": "modbus",
        "registers": registers,
        "register_blocks": blocks,
        "poll_groups": dict(DEFAULT_POLL_GROUPS),
    }


SMART_FAST_KEYS = {
    "battery_state_of_charge",
    "runtime_remaining",
    "battery_voltage",
    "load_percent",
    "input_voltage",
    "input_frequency",
}


def _smart_modbus_poll_group(metric_key: str) -> str:
    """Determine poll group for Smart-UPS modbus register."""
    return "fast" if metric_key in SMART_FAST_KEYS else "slow"


def get_smart_profile() -> dict[str, Any]:
    """Get profile for APC Smart-UPS devices (multi-source).

    Multi-source profile with modbus (primary) + snmp (metadata).
    Modbus provides real-time metrics, SNMP provides static metadata.

    Source: Legacy registers_smart_ups.py + APC PowerNet MIB
    """
    # Modbus registers (real-time metrics + configuration/diagnostics)
    modbus_registers = [
        # Raw status bitfield registers (decoded into derived state flags)
        {
            "key": "status_word_0",
            "address": 0x0000,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "status_word_1",
            "address": 0x0001,
            "count": 1,
            "type": "uint16",
            "poll_group": "fast",
        },
        {
            "key": "status_word_2",
            "address": 0x0002,
            "count": 1,
            "type": "uint16",
            "poll_group": "fast",
        },
        {
            "key": "status_word_3",
            "address": 0x0003,
            "count": 1,
            "type": "uint16",
            "poll_group": "fast",
        },
        # Normalized tier - fast poll (core operational metrics)
        {
            "key": "battery_state_of_charge",
            "address": 0x0005,
            "count": 1,
            "type": "uint16",
            "poll_group": "fast",
        },
        {
            "key": "runtime_remaining",
            "address": 0x0006,
            "count": 1,
            "type": "uint16",
            "poll_group": "fast",
        },
        {
            "key": "battery_voltage",
            "address": 0x0007,
            "count": 1,
            "type": "uint16",
            "poll_group": "fast",
        },
        {
            "key": "load_percent",
            "address": 0x000C,
            "count": 1,
            "type": "uint16",
            "poll_group": "fast",
        },
        {
            "key": "input_voltage",
            "address": 0x0011,
            "count": 1,
            "type": "uint16",
            "poll_group": "fast",
        },
        {
            "key": "input_frequency",
            "address": 0x0012,
            "count": 1,
            "type": "uint16",
            "poll_group": "fast",
        },
        # Extended tier - slow poll (diagnostics and configuration)
        {
            "key": "ups_internal_temperature",
            "address": 0x0008,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "load_amps",
            "address": 0x0009,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "actual_output_voltage",
            "address": 0x000E,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        # Extended tier - NEW validated expansion fields (slow poll)
        {
            "key": "line_quality",
            "address": 0x0004,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "bad_battery_packs",
            "address": 0x000A,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "total_battery_packs",
            "address": 0x000B,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "nominal_output_voltage",
            "address": 0x000D,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "max_input_voltage",
            "address": 0x000F,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "min_input_voltage",
            "address": 0x0010,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "lower_transfer_point",
            "address": 0x001B,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "upper_transfer_point",
            "address": 0x001C,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "shutdown_delay",
            "address": 0x001E,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "low_battery_duration",
            "address": 0x001F,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "turn_on_delay",
            "address": 0x0020,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "status_word_4",
            "address": 0x002A,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
        {
            "key": "status_word_5",
            "address": 0x002B,
            "count": 1,
            "type": "uint16",
            "poll_group": "slow",
        },
    ]

    # SNMP OIDs (static metadata)
    snmp_oids = {
        "model": {"oid": "1.3.6.1.4.1.318.1.1.1.1.1.1.0", "poll_group": "slow"},
        "serial_number": {"oid": "1.3.6.1.4.1.318.1.1.1.1.2.3.0", "poll_group": "slow"},
        "firmware_version": {
            "oid": "1.3.6.1.4.1.318.1.1.1.1.2.1.0",
            "poll_group": "slow",
        },
    }

    # Modbus register blocks for efficient batch reads
    # Block reads optimized to cover both normalized and extended tier registers
    modbus_blocks = [
        {
            "name": "status_and_battery_load",
            "start_address": 0x0000,
            "count": 19,  # 0x0000-0x0012
            "poll_group": "fast",
            # Consolidates prior core+load blocks to reduce per-cycle request count.
        },
        {
            "name": "configuration_and_extended_status",
            "start_address": 0x001B,
            "count": 17,  # 0x001B-0x002B
            "poll_group": "slow",
            # Consolidates prior slow blocks and tolerates sparse gap reads.
        },
    ]

    return {
        "profile_id": "apc_modbus_smart",
        "protocol": "multi_source",
        # Multi-source configuration
        "active_sources": {
            "modbus": {
                "enabled": True,
                "registers": modbus_registers,
                "register_blocks": modbus_blocks,
            },
            "snmp": {
                "enabled": True,
                "oids": snmp_oids,
            },
        },
        "poll_groups": dict(DEFAULT_POLL_GROUPS),
    }
