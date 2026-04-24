# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""UPS SNMP OID definitions.

Source: Legacy apps/ups-snmp-ha/custom_components/ups_snmp_ha/sensor_catalog_unified.py
Based on: RFC 1628 UPS MIB (1.3.6.1.2.1.33)
"""

from __future__ import annotations

# UPS-MIB OID Definitions
# Based on RFC 1628 - UPS Management Information Base

OIDS_UPS_MIB: dict[str, str] = {
    # Identity group (upsIdent)
    "manufacturer": "1.3.6.1.2.1.33.1.1.1.0",
    "model": "1.3.6.1.2.1.33.1.1.2.0",
    "firmware": "1.3.6.1.2.1.33.1.1.3.0",
    "name": "1.3.6.1.2.1.33.1.1.5.0",
    # Battery group (upsBattery)
    "battery_status": "1.3.6.1.2.1.33.1.2.1.0",
    "seconds_on_battery": "1.3.6.1.2.1.33.1.2.2.0",
    "runtime_remaining": "1.3.6.1.2.1.33.1.2.3.0",
    "battery_charge": "1.3.6.1.2.1.33.1.2.4.0",
    "battery_voltage": "1.3.6.1.2.1.33.1.2.5.0",
    "battery_temperature": "1.3.6.1.2.1.33.1.2.7.0",
    # Input group (upsInput)
    "input_line_count": "1.3.6.1.2.1.33.1.3.2.0",
    "input_frequency": "1.3.6.1.2.1.33.1.3.3.1.2.1",
    "input_voltage": "1.3.6.1.2.1.33.1.3.3.1.3.1",
    "input_current": "1.3.6.1.2.1.33.1.3.3.1.4.1",
    "input_power": "1.3.6.1.2.1.33.1.3.3.1.5.1",
    # Output group (upsOutput)
    "output_source": "1.3.6.1.2.1.33.1.4.1.0",
    "output_frequency": "1.3.6.1.2.1.33.1.4.2.0",
    "output_line_count": "1.3.6.1.2.1.33.1.4.3.0",
    "output_load": "1.3.6.1.2.1.33.1.4.4.1.5.1",
    # Bypass group (upsBypass)
    "bypass_frequency": "1.3.6.1.2.1.33.1.5.1.0",
    "bypass_line_count": "1.3.6.1.2.1.33.1.5.2.0",
    # Alarm group (upsAlarm)
    "alarms_present": "1.3.6.1.2.1.33.1.6.1.0",
}

# APC-MIB OID Definitions
# Based on APC PowerNet MIB (1.3.6.1.4.1.318.1.1.1)
# Source: Legacy sensor_catalog_unified.py ups_snmp_apc_mib section

OIDS_APC_MIB: dict[str, str] = {
    # Identity group (upsIdent - 1.3.6.1.4.1.318.1.1.1.1)
    "model": "1.3.6.1.4.1.318.1.1.1.1.1.1.0",
    "location": "1.3.6.1.4.1.318.1.1.1.1.1.2.0",
    "firmware": "1.3.6.1.4.1.318.1.1.1.1.2.1.0",
    "firmware_date": "1.3.6.1.4.1.318.1.1.1.1.2.2.0",
    "serial_number": "1.3.6.1.4.1.318.1.1.1.1.2.3.0",
    # Battery group (upsBattery - 1.3.6.1.4.1.318.1.1.1.2)
    "battery_status": "1.3.6.1.4.1.318.1.1.1.2.1.1.0",
    "battery_charge": "1.3.6.1.4.1.318.1.1.1.2.2.1.0",
    "battery_temperature": "1.3.6.1.4.1.318.1.1.1.2.2.2.0",
    "runtime_remaining": "1.3.6.1.4.1.318.1.1.1.2.2.3.0",
    # Input group (upsInput - 1.3.6.1.4.1.318.1.1.1.3)
    "input_voltage": "1.3.6.1.4.1.318.1.1.1.3.2.1.0",
    "input_frequency": "1.3.6.1.4.1.318.1.1.1.3.2.4.0",
    # Output group (upsOutput - 1.3.6.1.4.1.318.1.1.1.4)
    "output_source": "1.3.6.1.4.1.318.1.1.1.4.1.1.0",
    "output_voltage": "1.3.6.1.4.1.318.1.1.1.4.2.1.0",
    "output_frequency": "1.3.6.1.4.1.318.1.1.1.4.2.2.0",
    "output_load": "1.3.6.1.4.1.318.1.1.1.4.2.3.0",
}
