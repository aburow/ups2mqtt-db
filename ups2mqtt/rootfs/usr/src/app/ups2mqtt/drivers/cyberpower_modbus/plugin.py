# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .catalog import SINGLE_PHASE_CATALOG, THREE_PHASE_CATALOG
from .profiles import get_single_phase_profile, get_three_phase_profile
from ..base import DriverDescriptor


@dataclass(frozen=True, slots=True)
class CyberPowerModbusPluginManifest:
    family: str = "cyberpower_modbus"
    driver_ids: tuple[str, ...] = (
        "cyberpower_modbus_single_phase",
        "cyberpower_modbus_three_phase",
    )
    legacy_artifacts: dict[str, str] | None = None
    target_artifacts: dict[str, str] | None = None


MANIFEST = CyberPowerModbusPluginManifest(
    legacy_artifacts={
        "profiles": "apps/cyberpower-modbus-ha/custom_components/cyberpower_modbus/capability_profile_unified.py",
        "registers_single": "apps/cyberpower-modbus-ha/custom_components/cyberpower_modbus/registers_single_phase.py",
        "registers_three": "apps/cyberpower-modbus-ha/custom_components/cyberpower_modbus/registers_three_phase.py",
        "catalog": "apps/cyberpower-modbus-ha/custom_components/cyberpower_modbus/sensor_catalog_unified.py",
    },
    target_artifacts={
        "profiles": "ups2mqtt/drivers/cyberpower_modbus/profiles.py",
        "registers": "ups2mqtt/drivers/cyberpower_modbus/registers.py",
        "catalog": "ups2mqtt/drivers/cyberpower_modbus/catalog.py",
    },
)


DESCRIPTORS: dict[str, DriverDescriptor] = {
    "cyberpower_modbus_single_phase": DriverDescriptor(
        driver_id="cyberpower_modbus_single_phase",
        family="cyberpower_modbus_single_phase",
        transport="modbus",
        plugin_module=__name__,
        owns_runtime_metadata=True,
    ),
    "cyberpower_modbus_three_phase": DriverDescriptor(
        driver_id="cyberpower_modbus_three_phase",
        family="cyberpower_modbus_three_phase",
        transport="modbus",
        plugin_module=__name__,
        owns_runtime_metadata=True,
    ),
}


def get_capability_profile(driver_id: str) -> dict[str, Any]:
    """Return a deep-copied profile selected by CyberPower driver variant.

    Profile = Selection from catalog + polling metadata.
    Profile specifies which catalog entries apply to this model/variant
    and adds runtime polling configuration (poll_group, blocks, etc.).
    """
    if driver_id == "cyberpower_modbus_single_phase":
        return deepcopy(get_single_phase_profile())
    elif driver_id == "cyberpower_modbus_three_phase":
        return deepcopy(get_three_phase_profile())
    raise ValueError(f"No plugin capability profile for driver: {driver_id}")


def get_sensor_catalog(driver_id: str) -> dict[str, Any]:
    """Get per-driver catalog (exposure schema).

    Catalog = All exposable datapoints for this driver family.
    Defines canonical keys, source declarations, aliases, tiers, metadata.

    This is NOT runtime state. It is a schema that declares what CAN be exposed.
    Profile selects from catalog, runtime resolves from sources, discovery gates by tier.
    """
    if driver_id == "cyberpower_modbus_single_phase":
        return deepcopy(SINGLE_PHASE_CATALOG)
    elif driver_id == "cyberpower_modbus_three_phase":
        return deepcopy(THREE_PHASE_CATALOG)
    raise ValueError(f"No plugin catalog for driver: {driver_id}")
