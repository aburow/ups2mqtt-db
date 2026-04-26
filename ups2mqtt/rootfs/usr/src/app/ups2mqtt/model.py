# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

SensorPreferenceMap = dict[str, dict[str, bool | str]]


@dataclass(slots=True)
class DeviceConfig:
    id: str
    source: str
    host: str
    port: int = 502
    unit_id: int = 1
    snmp_community: str = "public"
    poll_interval: int | None = None
    name: str | None = None
    debug_logging: bool = False
    keep_connection_open: bool = False
    device_uid: str = ""
    discovery_enabled: bool = True
    polling_enabled: bool = True
    profile_uid: str = ""
    profile_mode: str = "local"
    local_profile_payload: dict[str, Any] | None = None
    local_selected_sensors: list[str] | None = None
    local_sensor_preferences: SensorPreferenceMap | None = None
    enable_extended_fields: bool = False

    def signature(
        self,
    ) -> tuple[
        str,
        str,
        int,
        int,
        str,
        int | None,
        str | None,
        bool,
        bool,
        str,
        bool,
        bool,
        str,
        str,
        str,
        tuple[str, ...],
        str,
        bool,
    ]:
        payload_signature = ""
        if isinstance(self.local_profile_payload, dict):
            payload_signature = json.dumps(self.local_profile_payload, sort_keys=True)
        sensor_signature = tuple(
            sorted(str(item) for item in (self.local_selected_sensors or []))
        )
        sensor_pref_signature = ""
        if isinstance(self.local_sensor_preferences, dict):
            sensor_pref_signature = json.dumps(
                self.local_sensor_preferences, sort_keys=True
            )
        return (
            self.source,
            self.host,
            self.port,
            self.unit_id,
            self.snmp_community,
            self.poll_interval,
            self.name,
            self.debug_logging,
            self.keep_connection_open,
            self.device_uid,
            self.discovery_enabled,
            self.polling_enabled,
            self.profile_uid,
            self.profile_mode,
            payload_signature,
            sensor_signature,
            sensor_pref_signature,
            self.enable_extended_fields,
        )


@dataclass(slots=True)
class AppConfig:
    mqtt_enabled: bool
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None
    mqtt_discovery_prefix: str
    mqtt_topic_prefix: str
    poll_interval: int
    poll_timeout: int
    max_concurrent_polls: int
    apps_dir: str
    web_enabled: bool
    web_host: str
    web_port: int
    devices: list[DeviceConfig]
    raw: dict[str, Any]
    ha_url: str | None = None
    ha_token: str | None = None
    ha_bridge_enabled: bool = False


@dataclass(slots=True)
class ProfileConfig:
    profile_uid: str
    name: str
    driver_key: str
    config_payload: dict[str, Any]
    selected_sensors: list[str]
    sensor_preferences: SensorPreferenceMap | None = None
    comments: str = ""
    is_protected: bool = False
