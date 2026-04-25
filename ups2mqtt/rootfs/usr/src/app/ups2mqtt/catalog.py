# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""Shared catalog sensor loading from the capability database."""

from __future__ import annotations

from typing import Any

from .capability_repository import get_capability_repository
from .drivers.registry import DRIVER_REGISTRY

CATALOG_DRIVER_SPECS: dict[str, tuple[str, str, str]] = {}
CATALOG_DRIVER_KEYS = set(DRIVER_REGISTRY.keys())


def get_catalog_keys(driver_key: str, apps_dir: str) -> set[str]:
    del apps_dir
    repo = get_capability_repository()
    rows = repo.load_catalog_sensor_rows(driver_key)
    return {
        key
        for item in rows
        for key in [str(item.get("key", "")).strip()]
        if key and not key.lower().endswith("_bf")
    }


def get_catalog_derived_metrics(driver_key: str, apps_dir: str) -> list[dict[str, Any]]:
    del apps_dir
    repo = get_capability_repository()
    return repo.load_catalog_derived_metrics(driver_key)


def get_catalog_sensor_rows(driver_key: str, apps_dir: str) -> list[dict[str, str]]:
    del apps_dir
    repo = get_capability_repository()
    return [
        {
            "key": str(item.get("key", "")),
            "label": str(item.get("label", "")),
            "category": str(item.get("category", "other")),
            "unit": str(item.get("unit", "")),
            "source": str(item.get("source", "")),
            "aliases": str(item.get("aliases", "")),
            "reference": str(item.get("reference", "")),
            "tier": str(item.get("tier", "normalized")),
        }
        for item in repo.load_catalog_sensor_rows(driver_key)
        if str(item.get("key", "")).strip()
        and not str(item.get("key", "")).strip().lower().endswith("_bf")
    ]
