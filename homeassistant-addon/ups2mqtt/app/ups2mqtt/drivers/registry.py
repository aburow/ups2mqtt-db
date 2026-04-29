# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from .base import DriverDescriptor

# Static registry scaffold only. No runtime wiring yet.
DRIVER_REGISTRY: dict[str, DriverDescriptor] = {
    "apc_modbus_smart": DriverDescriptor(
        driver_id="apc_modbus_smart",
        family="apc_smart",
        transport="multi_source",
        plugin_module="ups2mqtt.drivers.apc_modbus.plugin",
        owns_runtime_metadata=True,
        # Multi-source support
        supported_sources=("modbus", "snmp"),
        # UI metadata
        display_name="APC Smart-UPS (Modbus + SNMP)",
        vendor_display="APC",
        family_display="Smart-UPS",
        source_display="Modbus Primary, SNMP Fallback",
        search_aliases=("apc-smart-modbus-snmp", "smart-ups", "apc smart"),
    ),
    "apc_modbus_smt": DriverDescriptor(
        driver_id="apc_modbus_smt",
        family="apc_modbus_smt",
        transport="modbus",
        plugin_module="ups2mqtt.drivers.apc_modbus.plugin",
        owns_runtime_metadata=True,
    ),
    "apc_modbus_rack_pdu": DriverDescriptor(
        driver_id="apc_modbus_rack_pdu",
        family="apc_modbus_rack_pdu",
        transport="modbus",
        plugin_module="ups2mqtt.drivers.apc_modbus.plugin",
        owns_runtime_metadata=True,
    ),
    "cyberpower_modbus_single_phase": DriverDescriptor(
        driver_id="cyberpower_modbus_single_phase",
        family="cyberpower_modbus_single_phase",
        transport="modbus",
        plugin_module="ups2mqtt.drivers.cyberpower_modbus.plugin",
        owns_runtime_metadata=True,
    ),
    "cyberpower_modbus_three_phase": DriverDescriptor(
        driver_id="cyberpower_modbus_three_phase",
        family="cyberpower_modbus_three_phase",
        transport="modbus",
        plugin_module="ups2mqtt.drivers.cyberpower_modbus.plugin",
        owns_runtime_metadata=True,
    ),
    "ups_snmp_ups_mib": DriverDescriptor(
        driver_id="ups_snmp_ups_mib",
        family="ups_snmp_ups_mib",
        transport="snmp",
        plugin_module="ups2mqtt.drivers.ups_snmp.plugin",
        owns_runtime_metadata=True,
    ),
    "ups_snmp_apc_mib": DriverDescriptor(
        driver_id="ups_snmp_apc_mib",
        family="ups_snmp_apc_mib",
        transport="snmp",
        plugin_module="ups2mqtt.drivers.ups_snmp.plugin",
        owns_runtime_metadata=True,
    ),
}


def get_registered_driver_ids() -> list[str]:
    return sorted(DRIVER_REGISTRY.keys())
