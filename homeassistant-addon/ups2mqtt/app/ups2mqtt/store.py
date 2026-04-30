# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import threading
from uuid import uuid4

from .database import Database
from .model import DeviceConfig


class DeviceStore:
    def __init__(self, devices: list[DeviceConfig], db: Database) -> None:
        self._lock = threading.Lock()
        self._db = db
        # Keyed by device_uid — the stable identity for a device.
        self._devices: dict[str, DeviceConfig] = {
            device.device_uid: device for device in devices
        }

    def list_devices(self) -> list[DeviceConfig]:
        with self._lock:
            return list(self._devices.values())

    def get_by_uid(self, device_uid: str) -> DeviceConfig | None:
        with self._lock:
            return self._devices.get(device_uid)

    def get_by_id(self, device_id: str) -> DeviceConfig | None:
        """Look up a device by its mutable id label."""
        with self._lock:
            for device in self._devices.values():
                if device.id == device_id:
                    return device
            return None

    # Backward-compat alias used by callers that previously passed device.id.
    # Tries uid first, then falls back to id scan so existing call-sites keep working
    # until they are updated.
    def get(self, key: str) -> DeviceConfig | None:
        by_uid = self.get_by_uid(key)
        if by_uid is not None:
            return by_uid
        return self.get_by_id(key)

    def upsert(self, device: DeviceConfig) -> None:
        with self._lock:
            existing_uid = next(
                (
                    uid
                    for uid, existing in self._devices.items()
                    if existing.id == device.id
                ),
                None,
            )

            target_uid = device.device_uid
            if not target_uid:
                # Preserve identity across imports/updates that omit device_uid.
                target_uid = existing_uid or str(uuid4())

            if existing_uid and existing_uid != target_uid:
                self._devices.pop(existing_uid, None)
                self._db.delete_device(existing_uid)

            device = DeviceConfig(
                id=device.id,
                source=device.source,
                host=device.host,
                port=device.port,
                snmp_port=device.snmp_port,
                unit_id=device.unit_id,
                snmp_community=device.snmp_community,
                poll_interval=device.poll_interval,
                name=device.name,
                location=device.location,
                debug_logging=device.debug_logging,
                keep_connection_open=device.keep_connection_open,
                device_uid=target_uid,
                discovery_enabled=device.discovery_enabled,
                polling_enabled=device.polling_enabled,
                profile_uid=device.profile_uid,
                profile_mode=device.profile_mode,
                local_profile_payload=(
                    dict(device.local_profile_payload)
                    if isinstance(device.local_profile_payload, dict)
                    else None
                ),
                local_selected_sensors=(
                    [str(item) for item in device.local_selected_sensors]
                    if device.local_selected_sensors is not None
                    else None
                ),
                local_sensor_preferences=(
                    {
                        str(key): {
                            "mqtt_enabled": bool(values.get("mqtt_enabled", True)),
                            **(
                                {"poll_group": str(values.get("poll_group", "")).strip()}
                                if str(values.get("poll_group", "")).strip()
                                else {}
                            ),
                        }
                        for key, values in device.local_sensor_preferences.items()
                        if isinstance(key, str) and isinstance(values, dict)
                    }
                    if isinstance(device.local_sensor_preferences, dict)
                    else None
                ),
            )

            self._devices[target_uid] = device
            self._db.save_device(device)

    def delete_by_uid(self, device_uid: str) -> bool:
        with self._lock:
            if device_uid not in self._devices:
                return False
            self._devices.pop(device_uid)
            self._db.delete_device(device_uid)
            return True

    def delete_by_id(self, device_id: str) -> bool:
        """Delete a device by its mutable id label."""
        with self._lock:
            uid = next(
                (uid for uid, d in self._devices.items() if d.id == device_id),
                None,
            )
            if uid is None:
                return False
            self._devices.pop(uid)
            self._db.delete_device(uid)
            return True

    def delete(self, key: str) -> bool:
        """Backward-compat: tries uid first, then id."""
        if self.delete_by_uid(key):
            return True
        return self.delete_by_id(key)
