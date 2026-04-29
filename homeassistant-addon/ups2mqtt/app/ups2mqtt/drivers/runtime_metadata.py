# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import importlib
from typing import Any

from .registry import DRIVER_REGISTRY


def get_migrated_driver_ids() -> set[str]:
    return {
        driver_id
        for driver_id, descriptor in DRIVER_REGISTRY.items()
        if descriptor.owns_runtime_metadata
    }


def get_legacy_driver_ids() -> set[str]:
    return {
        driver_id
        for driver_id, descriptor in DRIVER_REGISTRY.items()
        if not descriptor.owns_runtime_metadata
    }


def driver_owns_runtime_metadata(driver_id: str) -> bool:
    descriptor = DRIVER_REGISTRY.get(driver_id)
    if descriptor is None:
        return False
    return bool(descriptor.owns_runtime_metadata)


def validate_driver_metadata_ownership() -> None:
    family_ownership: dict[str, bool] = {}
    for descriptor in DRIVER_REGISTRY.values():
        previous = family_ownership.get(descriptor.family)
        if previous is None:
            family_ownership[descriptor.family] = descriptor.owns_runtime_metadata
            continue
        if previous != descriptor.owns_runtime_metadata:
            raise RuntimeError(
                "Mixed metadata ownership in family "
                f"{descriptor.family}: all drivers in a family must share owns_runtime_metadata"
            )


def load_plugin_capability_profile(driver_id: str) -> dict[str, Any]:
    descriptor = DRIVER_REGISTRY.get(driver_id)
    if descriptor is None:
        raise ValueError(f"Unknown driver id for plugin profile load: {driver_id}")
    # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
    module = importlib.import_module(descriptor.plugin_module)
    loader = getattr(module, "get_capability_profile", None)
    if not callable(loader):
        raise ValueError(
            f"Driver plugin missing get_capability_profile: {descriptor.plugin_module}"
        )
    profile = loader(driver_id)
    if not isinstance(profile, dict):
        raise ValueError(
            f"Driver plugin returned invalid profile type for {driver_id}: "
            f"{type(profile).__name__}"
        )
    return dict(profile)


def load_plugin_sensor_catalog(driver_id: str) -> dict[str, Any]:
    descriptor = DRIVER_REGISTRY.get(driver_id)
    if descriptor is None:
        raise ValueError(f"Unknown driver id for plugin catalog load: {driver_id}")
    # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
    module = importlib.import_module(descriptor.plugin_module)
    loader = getattr(module, "get_sensor_catalog", None)
    if not callable(loader):
        raise ValueError(
            f"Driver plugin missing get_sensor_catalog: {descriptor.plugin_module}"
        )
    catalog = loader(driver_id)
    if not isinstance(catalog, dict):
        raise ValueError(
            f"Driver plugin returned invalid catalog type for {driver_id}: "
            f"{type(catalog).__name__}"
        )
    return dict(catalog)
