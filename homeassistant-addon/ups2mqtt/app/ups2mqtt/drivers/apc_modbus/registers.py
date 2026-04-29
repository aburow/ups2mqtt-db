# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""APC Modbus register definitions.

Source: Legacy apps/apc-modbus-ha/custom_components/apc_modbus/registers_smt_ups.py
Based on: 990-9840B-EN (Smart-UPS Models with prefix SMT, SMX, SURTD, and SRT)

Addresses are Modbus wire addresses (Absolute Starting Register Address 0 = Modicon 40001).
Scale: raw register value divided by scale gives the engineering-unit value.
"""

from __future__ import annotations

from typing import Any

# SMT Register Definitions
# Source: registers_smt_ups.py from legacy code

REGISTERS_SMT: list[dict[str, Any]] = [
    # Status bitfield registers
    {
        "key": "ups_status_bf",
        "address": 0x0000,
        "count": 2,
        "type": "uint32",
        "scale": 1,
    },
    {
        "key": "general_error_bf",
        "address": 0x0013,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    {
        "key": "power_system_error_bf",
        "address": 0x0014,
        "count": 2,
        "type": "uint32",
        "scale": 1,
    },
    {
        "key": "battery_system_error_bf",
        "address": 0x0016,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    # Measurement registers
    {
        "key": "runtime_remaining",
        "address": 0x0080,
        "count": 2,
        "type": "uint32",
        # Device reports seconds; normalize to canonical minutes for publication.
        "scale": 60,
    },
    {
        "key": "battery_state_of_charge",
        "address": 0x0082,
        "count": 1,
        "type": "uint16",
        "scale": 512,
    },
    {
        "key": "battery_voltage",
        "address": 0x0083,
        "count": 1,
        "type": "int16",
        "scale": 32,
    },
    {
        "key": "battery_voltage_negative",
        "address": 0x0084,
        "count": 1,
        "type": "int16",
        "scale": 32,
    },
    {
        "key": "battery_replacement_date_days",
        "address": 0x0085,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    {
        "key": "battery_temperature",
        "address": 0x0087,
        "count": 1,
        "type": "int16",
        "scale": 128,
    },
    {
        "key": "output_load_percent",
        "address": 0x0088,
        "count": 1,
        "type": "uint16",
        "scale": 256,
    },
    {
        "key": "output_load_percent_l2",
        "address": 0x0089,
        "count": 1,
        "type": "uint16",
        "scale": 256,
    },
    {
        "key": "output_apparent_power_percent",
        "address": 0x008A,
        "count": 1,
        "type": "uint16",
        "scale": 256,
    },
    {
        "key": "output_apparent_power_percent_l2",
        "address": 0x008B,
        "count": 1,
        "type": "uint16",
        "scale": 256,
    },
    {
        "key": "output_current",
        "address": 0x008C,
        "count": 1,
        "type": "uint16",
        "scale": 32,
    },
    {
        "key": "output_current_l2",
        "address": 0x008D,
        "count": 1,
        "type": "uint16",
        "scale": 32,
    },
    {
        "key": "output_voltage",
        "address": 0x008E,
        "count": 1,
        "type": "uint16",
        "scale": 64,
    },
    {
        "key": "output_voltage_l2",
        "address": 0x008F,
        "count": 1,
        "type": "uint16",
        "scale": 64,
    },
    {
        "key": "output_frequency",
        "address": 0x0090,
        "count": 1,
        "type": "uint16",
        "scale": 128,
    },
    {
        "key": "output_energy",
        "address": 0x0091,
        "count": 2,
        "type": "uint32",
        "scale": 1,
    },
    {
        "key": "bypass_input_status_bf",
        "address": 0x0093,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    {
        "key": "bypass_voltage",
        "address": 0x0094,
        "count": 1,
        "type": "uint16",
        "scale": 64,
    },
    {
        "key": "bypass_frequency",
        "address": 0x0095,
        "count": 1,
        "type": "uint16",
        "scale": 128,
    },
    {
        "key": "input_status_bf",
        "address": 0x0096,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    {
        "key": "input_voltage",
        "address": 0x0097,
        "count": 1,
        "type": "uint16",
        "scale": 64,
    },
    {
        "key": "input_voltage_l2",
        "address": 0x0098,
        "count": 1,
        "type": "uint16",
        "scale": 64,
    },
    {
        "key": "input_voltage_l3",
        "address": 0x0099,
        "count": 1,
        "type": "uint16",
        "scale": 64,
    },
]

REGISTER_BLOCKS_SMT: list[dict[str, Any]] = [
    {
        "name": "status",
        "start_address": 0x0000,
        "count": 23,
    },
    {
        "name": "measurements",
        "start_address": 0x0080,
        "count": 26,
    },
]

# Rack PDU Register Definitions
# Source: registers_rack_pdu.py from legacy code
# Based on: APC NetShelter Rack PDU register maps

REGISTERS_RACK_PDU: list[dict[str, Any]] = [
    # Capability registers
    {
        "key": "num_phases",
        "address": 0x009E,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    {
        "key": "num_metered_phases",
        "address": 0x009F,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    {
        "key": "num_banks",
        "address": 0x00A0,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    {
        "key": "num_outlets",
        "address": 0x00A1,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    {
        "key": "num_metered_outlets",
        "address": 0x00A2,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    # Device-level measurements
    {
        "key": "device_real_power",
        "address": 0x00CF,
        "count": 1,
        "type": "int16",
        "scale": 100,
    },
    {
        "key": "device_apparent_power",
        "address": 0x00D0,
        "count": 1,
        "type": "int16",
        "scale": 100,
    },
    {
        "key": "device_power_factor",
        "address": 0x00D1,
        "count": 1,
        "type": "int16",
        "scale": 100,
    },
    {
        "key": "device_energy",
        "address": 0x00D2,
        "count": 2,
        "type": "uint32",
        "scale": 10,
    },
    {
        "key": "device_load_state",
        "address": 0x00D4,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    # Phase L1 measurements
    {
        "key": "phase_L1_current",
        "address": 0x029B,
        "count": 1,
        "type": "int16",
        "scale": 10,
    },
    {
        "key": "phase_L1_voltage",
        "address": 0x029C,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
    {
        "key": "phase_L1_real_power",
        "address": 0x029D,
        "count": 1,
        "type": "int16",
        "scale": 1,
    },
    {
        "key": "phase_L1_apparent_power",
        "address": 0x029E,
        "count": 1,
        "type": "int16",
        "scale": 1,
    },
    {
        "key": "phase_L1_power_factor",
        "address": 0x029F,
        "count": 1,
        "type": "int16",
        "scale": 100,
    },
    {
        "key": "phase_L1_state",
        "address": 0x02A0,
        "count": 1,
        "type": "uint16",
        "scale": 1,
    },
]

REGISTER_BLOCKS_RACK_PDU: list[dict[str, Any]] = [
    {
        "name": "capabilities",
        "start_address": 0x009E,
        "count": 5,
    },
    {
        "name": "device_measurements",
        "start_address": 0x00CF,
        "count": 7,
    },
    {
        "name": "phase_L1_measurements",
        "start_address": 0x029B,
        "count": 6,
    },
]
