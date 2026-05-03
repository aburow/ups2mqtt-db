# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""CyberPower Modbus profiles - selection from catalog for specific models/variants.

PROFILE RESPONSIBILITY:
=======================

A profile is NOT an independent schema. It is a SELECTION/FILTER that specifies:
1. Which catalog entries apply to this specific UPS model/variant
2. Polling metadata (poll_group, word_order) for runtime optimization
3. Register blocks for efficient batch reads

The profile references catalog entries by their keys (raw register keys in this case,
which are mapped to canonical keys via aliases in the catalog).

Profile keys use raw register naming (battery_capacity, utility_voltage) because
these come from registers.py. The catalog provides canonical names (battery_state_of_charge,
input_voltage) and aliases to bridge the two namespaces.

ARCHITECTURAL FLOW:
===================
catalog.py: All exposable datapoints with canonical keys + aliases
    ↓
profiles.py: Selects applicable entries, adds polling metadata
    ↓
resolver.py: Filters by tier (normalized vs extended)
    ↓
pollers.py: Resolves from declared sources (register, OID)
    ↓
discovery: Publishes according to tier and configuration

See catalog.py module docstring for full architectural model.
"""

from __future__ import annotations

from typing import Any

from ...constants import DEFAULT_POLL_INTERVAL_SECONDS
from .registers import (
    REGISTER_BLOCKS_SINGLE_PHASE,
    REGISTER_BLOCKS_THREE_PHASE,
    REGISTERS_SINGLE_PHASE,
    REGISTERS_THREE_PHASE,
)

DEFAULT_POLL_GROUPS: dict[str, dict[str, int]] = {
    "fast": {"interval_s": DEFAULT_POLL_INTERVAL_SECONDS},
    "slow": {"interval_s": 60},
}

SINGLE_PHASE_FAST_KEYS = {
    "runtime_remaining",
    "battery_capacity",
    "utility_voltage",
    "output_voltage",
    "output_load_percent",
    "battery_discharging",
}


def _register_poll_group(metric_key: str) -> str:
    return "fast" if metric_key in SINGLE_PHASE_FAST_KEYS else "slow"


def _block_poll_group(block_name: str) -> str:
    name = block_name.lower()
    if "measurement" in name or "battery" in name:
        return "fast"
    return "slow"


def get_single_phase_profile() -> dict[str, Any]:
    registers: list[dict[str, Any]] = []
    for register in REGISTERS_SINGLE_PHASE:
        item = dict(register)
        item["poll_group"] = _register_poll_group(str(item["key"]))
        if "word_order" not in item:
            item["word_order"] = "big"
        registers.append(item)

    blocks: list[dict[str, Any]] = []
    for block in REGISTER_BLOCKS_SINGLE_PHASE:
        blocks.append(
            {
                "name": block["name"],
                "start_address": block["start_address"],
                "count": block["count"],
                "poll_group": _block_poll_group(str(block["name"])),
            }
        )

    return {
        "profile_id": "cyberpower_modbus_single_phase",
        "protocol": "modbus",
        "registers": registers,
        "register_blocks": blocks,
        "poll_groups": dict(DEFAULT_POLL_GROUPS),
    }


# Three-Phase Profile
# Source: legacy registers_three_phase.py

THREE_PHASE_FAST_KEYS = {
    "battery_voltage",
    "battery_current",
    "battery_capacity",
    "battery_runtime_remaining",
    "input_voltage_phase_a",
    "input_voltage_phase_b",
    "input_voltage_phase_c",
    "output_voltage_phase_a",
    "output_voltage_phase_b",
    "output_voltage_phase_c",
    "output_current_phase_a",
    "output_current_phase_b",
    "output_current_phase_c",
    "output_active_power_phase_a",
    "output_active_power_phase_b",
    "output_active_power_phase_c",
    "load_percent_phase_a",
    "load_percent_phase_b",
    "load_percent_phase_c",
}


def _three_phase_register_poll_group(metric_key: str) -> str:
    return "fast" if metric_key in THREE_PHASE_FAST_KEYS else "slow"


def _three_phase_block_poll_group(block_name: str) -> str:
    name = block_name.lower()
    if "measurement" in name:
        return "fast"
    return "slow"


def get_three_phase_profile() -> dict[str, Any]:
    """Get profile for three-phase UPS devices.

    Profile = Selection from catalog + polling metadata.
    Selects all fields from REGISTERS_THREE_PHASE and adds polling configuration.
    """
    registers: list[dict[str, Any]] = []
    for register in REGISTERS_THREE_PHASE:
        item = dict(register)
        item["poll_group"] = _three_phase_register_poll_group(str(item["key"]))
        if "word_order" not in item:
            item["word_order"] = "big"
        registers.append(item)

    blocks: list[dict[str, Any]] = []
    for block in REGISTER_BLOCKS_THREE_PHASE:
        blocks.append(
            {
                "name": block["name"],
                "start_address": block["start_address"],
                "count": block["count"],
                "poll_group": _three_phase_block_poll_group(str(block["name"])),
            }
        )

    return {
        "profile_id": "cyberpower_modbus_three_phase",
        "protocol": "modbus",
        "registers": registers,
        "register_blocks": blocks,
        "poll_groups": dict(DEFAULT_POLL_GROUPS),
    }
