# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .catalog import APC_RACK_PDU_CATALOG, APC_SMART_CATALOG, APC_SMT_CATALOG
from .profiles import get_rack_pdu_profile, get_smart_profile, get_smt_profile
from ..base import DriverDescriptor


@dataclass(frozen=True, slots=True)
class APCModbusPluginManifest:
    family: str = "apc_modbus"
    driver_ids: tuple[str, ...] = (
        "apc_modbus_smart",
        "apc_modbus_smt",
        "apc_modbus_rack_pdu",
    )
    # Current artifact locations consumed by legacy runtime paths.
    legacy_artifacts: dict[str, str] | None = None
    # Target in-repo ownership locations.
    target_artifacts: dict[str, str] | None = None


MANIFEST = APCModbusPluginManifest(
    legacy_artifacts={
        "profiles": "apps/apc-modbus-ha/custom_components/apc_modbus/capability_profiles_unified.py",
        "registers_smart": "apps/apc-modbus-ha/custom_components/apc_modbus/registers_smart_ups.py",
        "registers_smt": "apps/apc-modbus-ha/custom_components/apc_modbus/registers_smt_ups.py",
        "registers_rack_pdu": "apps/apc-modbus-ha/custom_components/apc_modbus/registers_rack_pdu.py",
        "catalog": "apps/apc-modbus-ha/custom_components/apc_modbus/sensor_catalog_unified.py",
    },
    target_artifacts={
        "profiles": "ups2mqtt/drivers/apc_modbus/profiles.py",
        "registers": "ups2mqtt/drivers/apc_modbus/registers.py",
        "catalog": "ups2mqtt/drivers/apc_modbus/catalog.py",
    },
)


DESCRIPTORS: dict[str, DriverDescriptor] = {
    "apc_modbus_smart": DriverDescriptor(
        driver_id="apc_modbus_smart",
        family="apc_modbus",
        transport="hybrid",
        plugin_module=__name__,
        owns_runtime_metadata=True,
    ),
    "apc_modbus_smt": DriverDescriptor(
        driver_id="apc_modbus_smt",
        family="apc_modbus_smt",
        transport="modbus",
        plugin_module=__name__,
        owns_runtime_metadata=True,
    ),
    "apc_modbus_rack_pdu": DriverDescriptor(
        driver_id="apc_modbus_rack_pdu",
        family="apc_modbus_rack_pdu",
        transport="modbus",
        plugin_module=__name__,
        owns_runtime_metadata=True,
    ),
}


def get_capability_profile(driver_id: str) -> dict[str, Any]:
    """Return a deep-copied runtime profile, embedding catalog for multi-source drivers.

    Profile = Selection from catalog + polling metadata.
    Profile specifies which catalog entries apply to this model/variant
    and adds runtime polling configuration (poll_group, blocks, etc.).

    For multi-source drivers, catalog is embedded in profile for runtime
    tier-aware planning access.
    """
    if driver_id == "apc_modbus_smt":
        profile = deepcopy(get_smt_profile())
        # SMT is a multi_source driver and requires embedded catalog for
        # tier-aware transport planning in pollers._poll_multi_source.
        profile["_catalog"] = deepcopy(APC_SMT_CATALOG)
        return profile
    if driver_id == "apc_modbus_rack_pdu":
        return deepcopy(get_rack_pdu_profile())
    if driver_id == "apc_modbus_smart":
        profile = deepcopy(get_smart_profile())
        # Embed catalog for runtime tier filtering
        # Multi-source drivers need catalog access during planning to filter
        # fields by tier before transport split
        profile["_catalog"] = deepcopy(APC_SMART_CATALOG)
        return profile
    raise NotImplementedError(
        f"Scaffold only for {driver_id}; runtime still uses legacy capability loader"
    )


def get_sensor_catalog(driver_id: str) -> dict[str, Any]:
    """Get per-driver catalog (exposure schema).

    Catalog = All exposable datapoints for this driver family.
    Defines canonical keys, source declarations, tiers, metadata.

    This is NOT runtime state. It is a schema that declares what CAN be exposed.
    Profile selects from catalog, runtime resolves from sources, discovery gates by tier.
    """
    if driver_id == "apc_modbus_smt":
        return deepcopy(APC_SMT_CATALOG)
    if driver_id == "apc_modbus_rack_pdu":
        return deepcopy(APC_RACK_PDU_CATALOG)
    if driver_id == "apc_modbus_smart":
        return deepcopy(APC_SMART_CATALOG)
    raise NotImplementedError(
        f"Scaffold only for {driver_id}; runtime still uses legacy catalog loader"
    )
