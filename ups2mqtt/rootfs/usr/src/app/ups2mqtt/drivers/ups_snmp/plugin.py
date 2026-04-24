# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .catalog import APC_MIB_CATALOG, UPS_MIB_CATALOG
from .profiles import get_apc_mib_profile, get_ups_mib_profile
from ..base import DriverDescriptor


@dataclass(frozen=True, slots=True)
class UpsSnmpPluginManifest:
    family: str = "ups_snmp"
    driver_ids: tuple[str, ...] = ("ups_snmp_ups_mib", "ups_snmp_apc_mib")
    legacy_artifacts: dict[str, str] | None = None
    target_artifacts: dict[str, str] | None = None


MANIFEST = UpsSnmpPluginManifest(
    legacy_artifacts={
        "profiles": "apps/ups-snmp-ha/custom_components/ups_snmp_ha/capability_profile_unified.py",
        "oids_and_derived": "apps/ups-snmp-ha/custom_components/ups_snmp_ha/coordinator.py",
        "catalog": "apps/ups-snmp-ha/custom_components/ups_snmp_ha/sensor_catalog_unified.py",
    },
    target_artifacts={
        "profiles": "ups2mqtt/drivers/ups_snmp/profiles.py",
        "oids": "ups2mqtt/drivers/ups_snmp/oids.py",
        "catalog": "ups2mqtt/drivers/ups_snmp/catalog.py",
    },
)


DESCRIPTORS: dict[str, DriverDescriptor] = {
    "ups_snmp_ups_mib": DriverDescriptor(
        driver_id="ups_snmp_ups_mib",
        family="ups_snmp_ups_mib",
        transport="snmp",
        plugin_module=__name__,
        owns_runtime_metadata=True,
    ),
    "ups_snmp_apc_mib": DriverDescriptor(
        driver_id="ups_snmp_apc_mib",
        family="ups_snmp",
        transport="snmp",
        plugin_module=__name__,
        owns_runtime_metadata=False,
    ),
}


def get_capability_profile(driver_id: str) -> dict[str, Any]:
    """Return a deep-copied SNMP profile for the requested MIB variant.

    Profile = Selection from catalog + polling metadata.
    Profile specifies which catalog entries apply to this MIB/variant
    and adds runtime polling configuration (poll_group, etc.).
    """
    if driver_id == "ups_snmp_ups_mib":
        return deepcopy(get_ups_mib_profile())
    if driver_id == "ups_snmp_apc_mib":
        return deepcopy(get_apc_mib_profile())
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
    if driver_id == "ups_snmp_ups_mib":
        return deepcopy(UPS_MIB_CATALOG)
    if driver_id == "ups_snmp_apc_mib":
        return deepcopy(APC_MIB_CATALOG)
    raise NotImplementedError(
        f"Scaffold only for {driver_id}; runtime still uses legacy catalog loader"
    )
