# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class DriverDescriptor:
    """Minimal static metadata describing a registered driver plugin."""

    driver_id: str
    family: str
    transport: str
    plugin_module: str
    owns_runtime_metadata: bool

    # Multi-source support
    supported_sources: tuple[str, ...] | None = None

    # UI/Display metadata
    display_name: str | None = None
    vendor_display: str | None = None
    family_display: str | None = None
    source_display: str | None = None
    search_aliases: tuple[str, ...] | None = None
    legacy_driver_ids: tuple[str, ...] | None = None


class DriverPlugin(Protocol):
    """Future stable plugin contract for driver units.

    This is scaffolding only. Core runtime continues to use existing code paths.
    """

    descriptor: DriverDescriptor

    def get_capability_profile(self) -> dict[str, Any]:
        """Return fully self-contained runtime profile for this driver."""

    def get_sensor_catalog(self) -> dict[str, Any]:
        """Return fully self-contained semantic catalog for this driver."""
