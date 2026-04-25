# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""SQLite database for device persistence."""

from __future__ import annotations

import os
import sqlite3
import threading
import json

from .model import DeviceConfig, ProfileConfig

DEFAULT_PROTECTED_PROFILES: frozenset[tuple[str, str]] = frozenset(
    {
        ("APC Rack PDU MODBUS", "apc_modbus_rack_pdu"),
        ("CyberPower Single Phase", "cyberpower_modbus_single_phase"),
        ("Legacy APC", "ups_snmp_apc_mib"),
        ("Legacy SmartUPS MODBUS", "apc_modbus_smart"),
        ("SMT1500", "apc_modbus_smt"),
        ("Standards Base UPS", "ups_snmp_ups_mib"),
    }
)
_DEVICE_COLUMN_MIGRATIONS: dict[str, str] = {
    "keep_connection_open": (
        "ALTER TABLE devices ADD COLUMN keep_connection_open INTEGER NOT NULL DEFAULT 0"
    ),
    "profile_uid": "ALTER TABLE devices ADD COLUMN profile_uid TEXT NOT NULL DEFAULT ''",
    "profile_mode": (
        "ALTER TABLE devices ADD COLUMN profile_mode TEXT NOT NULL DEFAULT 'local'"
    ),
    "local_profile_payload": "ALTER TABLE devices ADD COLUMN local_profile_payload TEXT",
    "local_selected_sensors": (
        "ALTER TABLE devices ADD COLUMN local_selected_sensors TEXT"
    ),
    "local_sensor_preferences": (
        "ALTER TABLE devices ADD COLUMN local_sensor_preferences TEXT"
    ),
}
_PROFILE_COLUMN_MIGRATIONS: dict[str, str] = {
    "comments": "ALTER TABLE profiles ADD COLUMN comments TEXT",
    "is_protected": (
        "ALTER TABLE profiles ADD COLUMN is_protected INTEGER NOT NULL DEFAULT 0"
    ),
    "sensor_preferences": "ALTER TABLE profiles ADD COLUMN sensor_preferences TEXT",
}


class Database:
    """Thread-safe SQLite database for device storage."""

    def __init__(self, db_path: str = "/data/ups2mqtt.db"):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                self.db_path, check_same_thread=False, timeout=30.0
            )
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self) -> None:
        """Initialize database schema."""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Devices table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_uid TEXT PRIMARY KEY,
                id TEXT NOT NULL,
                source TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                unit_id INTEGER NOT NULL,
                snmp_community TEXT NOT NULL,
                poll_interval INTEGER,
                name TEXT,
                debug_logging INTEGER NOT NULL DEFAULT 0,
                keep_connection_open INTEGER NOT NULL DEFAULT 0,
                discovery_enabled INTEGER NOT NULL DEFAULT 1,
                polling_enabled INTEGER NOT NULL DEFAULT 1,
                profile_uid TEXT NOT NULL DEFAULT '',
                profile_mode TEXT NOT NULL DEFAULT 'local',
                local_profile_payload TEXT,
                local_selected_sensors TEXT,
                local_sensor_preferences TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self._ensure_column(
            cursor=cursor,
            table="devices",
            column="keep_connection_open",
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column(
            cursor=cursor,
            table="devices",
            column="profile_uid",
            definition="TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            cursor=cursor,
            table="devices",
            column="profile_mode",
            definition="TEXT NOT NULL DEFAULT 'local'",
        )
        self._ensure_column(
            cursor=cursor,
            table="devices",
            column="local_profile_payload",
            definition="TEXT",
        )
        self._ensure_column(
            cursor=cursor,
            table="devices",
            column="local_selected_sensors",
            definition="TEXT",
        )
        self._ensure_column(
            cursor=cursor,
            table="devices",
            column="local_sensor_preferences",
            definition="TEXT",
        )

        # Index for fast lookup by id
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_devices_id ON devices(id)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                profile_uid TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                driver_key TEXT NOT NULL,
                config_payload TEXT NOT NULL,
                selected_sensors TEXT NOT NULL,
                sensor_preferences TEXT,
                comments TEXT,
                is_protected INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._ensure_column(
            cursor=cursor,
            table="profiles",
            column="comments",
            definition="TEXT",
        )
        self._ensure_column(
            cursor=cursor,
            table="profiles",
            column="is_protected",
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column(
            cursor=cursor,
            table="profiles",
            column="sensor_preferences",
            definition="TEXT",
        )

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_profiles_name ON profiles(name)
        """)

        self._init_capability_schema(cursor)

        # Honor DISABLE_PROFILE_PROTECTION env var for development
        if self._is_profile_protection_disabled():
            cursor.execute("UPDATE profiles SET is_protected = 0")
        else:
            self._mark_default_profiles_protected(cursor)
        conn.commit()

    @staticmethod
    def _init_capability_schema(cursor: sqlite3.Cursor) -> None:
        """Initialize normalized capability metadata schema."""
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS capability_transports (
                name TEXT PRIMARY KEY,
                runtime_supported INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS capability_drivers (
                driver_key TEXT PRIMARY KEY,
                family TEXT NOT NULL,
                protocol TEXT NOT NULL,
                transport TEXT NOT NULL,
                owns_runtime_metadata INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                display_name TEXT,
                vendor_display TEXT,
                family_display TEXT,
                source_display TEXT,
                search_aliases_json TEXT NOT NULL DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS capability_driver_transports (
                driver_key TEXT NOT NULL,
                transport_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, transport_name),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE,
                FOREIGN KEY(transport_name) REFERENCES capability_transports(name) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_driver_profiles (
                driver_key TEXT PRIMARY KEY,
                profile_json TEXT NOT NULL,
                profile_hash TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_poll_groups (
                driver_key TEXT NOT NULL,
                group_name TEXT NOT NULL,
                interval_s INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, group_name),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_modbus_mappings (
                driver_key TEXT NOT NULL,
                transport_name TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                address INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                data_type TEXT NOT NULL,
                scale REAL NOT NULL DEFAULT 1,
                word_order TEXT NOT NULL DEFAULT 'big',
                poll_group TEXT NOT NULL DEFAULT 'slow',
                spec_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, transport_name, sensor_key, address),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_snmp_mappings (
                driver_key TEXT NOT NULL,
                transport_name TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                oid TEXT NOT NULL,
                poll_group TEXT NOT NULL DEFAULT 'slow',
                timeticks_minutes INTEGER NOT NULL DEFAULT 0,
                spec_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, transport_name, sensor_key, oid),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_bacnet_mappings (
                driver_key TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                object_type TEXT NOT NULL DEFAULT '',
                object_instance INTEGER NOT NULL DEFAULT 0,
                property_name TEXT NOT NULL DEFAULT '',
                poll_group TEXT NOT NULL DEFAULT 'slow',
                spec_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, sensor_key, object_type, object_instance, property_name),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_rest_mappings (
                driver_key TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT 'GET',
                endpoint TEXT NOT NULL DEFAULT '',
                json_path TEXT NOT NULL DEFAULT '',
                poll_group TEXT NOT NULL DEFAULT 'slow',
                spec_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, sensor_key, method, endpoint, json_path),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_register_blocks (
                driver_key TEXT NOT NULL,
                transport_name TEXT NOT NULL,
                name TEXT NOT NULL,
                start_address INTEGER NOT NULL,
                count INTEGER NOT NULL,
                poll_group TEXT NOT NULL DEFAULT 'slow',
                spec_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, transport_name, name, start_address),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_snmp_blocks (
                driver_key TEXT NOT NULL,
                transport_name TEXT NOT NULL,
                name TEXT NOT NULL,
                poll_group TEXT NOT NULL DEFAULT 'slow',
                metrics_json TEXT NOT NULL DEFAULT '[]',
                spec_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, transport_name, name),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_key_precedence (
                driver_key TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                preferred_source TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, sensor_key),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_sensors (
                driver_key TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                label TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                unit TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                aliases_json TEXT NOT NULL DEFAULT '[]',
                reference TEXT NOT NULL DEFAULT '',
                tier TEXT NOT NULL DEFAULT 'normalized',
                note TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                spec_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, sensor_key),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_derived_metrics (
                driver_key TEXT NOT NULL,
                metric_key TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, metric_key),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_value_maps (
                driver_key TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                raw_value TEXT NOT NULL,
                display_text TEXT NOT NULL,
                publish_raw INTEGER NOT NULL DEFAULT 1,
                text_suffix TEXT NOT NULL DEFAULT '_text',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, sensor_key, raw_value),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_bitfield_flags (
                driver_key TEXT NOT NULL,
                source_key TEXT NOT NULL,
                bit_index INTEGER NOT NULL,
                flag_key TEXT NOT NULL,
                label TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'status',
                tier TEXT NOT NULL DEFAULT 'extended',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, source_key, bit_index, flag_key),
                FOREIGN KEY(driver_key) REFERENCES capability_drivers(driver_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capability_metric_contracts (
                metric_key TEXT PRIMARY KEY,
                value_kind TEXT NOT NULL,
                canonical_unit TEXT NOT NULL DEFAULT '',
                default_multiplier REAL NOT NULL DEFAULT 1,
                default_offset REAL NOT NULL DEFAULT 0,
                default_unit TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS capability_seed_version (
                seed_key TEXT PRIMARY KEY,
                seed_hash TEXT NOT NULL,
                seeded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS capability_sensor_overrides (
                driver_key TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                override_json TEXT NOT NULL DEFAULT '{}',
                is_deleted INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, sensor_key)
            );

            CREATE TABLE IF NOT EXISTS capability_key_precedence_overrides (
                driver_key TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                preferred_source TEXT NOT NULL DEFAULT '',
                is_deleted INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, sensor_key)
            );

            CREATE TABLE IF NOT EXISTS capability_mapping_overrides (
                driver_key TEXT NOT NULL,
                transport_name TEXT NOT NULL,
                mapping_kind TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                match_value TEXT NOT NULL,
                override_json TEXT NOT NULL DEFAULT '{}',
                is_deleted INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (
                    driver_key, transport_name, mapping_kind, sensor_key, match_value
                )
            );

            CREATE TABLE IF NOT EXISTS capability_value_map_overrides (
                driver_key TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                raw_value TEXT NOT NULL,
                display_text TEXT NOT NULL DEFAULT '',
                is_deleted INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, sensor_key, raw_value)
            );

            CREATE TABLE IF NOT EXISTS capability_bitfield_flag_overrides (
                driver_key TEXT NOT NULL,
                source_key TEXT NOT NULL,
                bit_index INTEGER NOT NULL,
                flag_key TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'status',
                tier TEXT NOT NULL DEFAULT 'extended',
                is_deleted INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (driver_key, source_key, bit_index, flag_key)
            );

            CREATE INDEX IF NOT EXISTS idx_capability_sensors_driver_position
            ON capability_sensors(driver_key, position, sensor_key);

            CREATE INDEX IF NOT EXISTS idx_capability_modbus_driver
            ON capability_modbus_mappings(driver_key, transport_name);

            CREATE INDEX IF NOT EXISTS idx_capability_snmp_driver
            ON capability_snmp_mappings(driver_key, transport_name);

            CREATE INDEX IF NOT EXISTS idx_capability_value_maps_driver
            ON capability_value_maps(driver_key, sensor_key);

            CREATE INDEX IF NOT EXISTS idx_capability_bitfield_flags_driver
            ON capability_bitfield_flags(driver_key, source_key, bit_index);
            """
        )

    # Device operations

    def save_device(self, device: DeviceConfig) -> None:
        """Save or update a device."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO devices (
                device_uid, id, source, host, port, unit_id, snmp_community,
                poll_interval, name, debug_logging, keep_connection_open,
                discovery_enabled, polling_enabled,
                profile_uid, profile_mode, local_profile_payload, local_selected_sensors, local_sensor_preferences,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_uid) DO UPDATE SET
                id = excluded.id,
                source = excluded.source,
                host = excluded.host,
                port = excluded.port,
                unit_id = excluded.unit_id,
                snmp_community = excluded.snmp_community,
                poll_interval = excluded.poll_interval,
                name = excluded.name,
                debug_logging = excluded.debug_logging,
                keep_connection_open = excluded.keep_connection_open,
                discovery_enabled = excluded.discovery_enabled,
                polling_enabled = excluded.polling_enabled,
                profile_uid = excluded.profile_uid,
                profile_mode = excluded.profile_mode,
                local_profile_payload = excluded.local_profile_payload,
                local_selected_sensors = excluded.local_selected_sensors,
                local_sensor_preferences = excluded.local_sensor_preferences,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                device.device_uid,
                device.id,
                device.source,
                device.host,
                device.port,
                device.unit_id,
                device.snmp_community,
                device.poll_interval,
                device.name,
                1 if device.debug_logging else 0,
                1 if device.keep_connection_open else 0,
                1 if device.discovery_enabled else 0,
                1 if device.polling_enabled else 0,
                device.profile_uid,
                device.profile_mode,
                json.dumps(device.local_profile_payload, sort_keys=True)
                if isinstance(device.local_profile_payload, dict)
                else None,
                json.dumps(
                    [str(item) for item in (device.local_selected_sensors or [])]
                )
                if device.local_selected_sensors is not None
                else None,
                json.dumps(device.local_sensor_preferences, sort_keys=True)
                if isinstance(device.local_sensor_preferences, dict)
                else None,
            ),
        )
        conn.commit()

    def load_devices(self) -> list[DeviceConfig]:
        """Load all devices from database."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM devices ORDER BY created_at")
        rows = cursor.fetchall()

        devices = []
        for row in rows:
            local_profile_payload: dict[str, object] | None = None
            if row["local_profile_payload"]:
                try:
                    parsed_payload = json.loads(str(row["local_profile_payload"]))
                    if isinstance(parsed_payload, dict):
                        local_profile_payload = {
                            str(key): value for key, value in parsed_payload.items()
                        }
                except (TypeError, ValueError, json.JSONDecodeError):
                    local_profile_payload = None
            local_selected_sensors: list[str] | None = None
            if row["local_selected_sensors"]:
                try:
                    parsed_sensors = json.loads(str(row["local_selected_sensors"]))
                    if isinstance(parsed_sensors, list):
                        local_selected_sensors = [
                            str(item) for item in parsed_sensors if str(item)
                        ]
                except (TypeError, ValueError, json.JSONDecodeError):
                    local_selected_sensors = None
            local_sensor_preferences: dict[str, dict[str, bool]] | None = None
            if row["local_sensor_preferences"]:
                try:
                    parsed_preferences = json.loads(
                        str(row["local_sensor_preferences"])
                    )
                    if isinstance(parsed_preferences, dict):
                        local_sensor_preferences = {}
                        for key, raw in parsed_preferences.items():
                            if not isinstance(key, str) or not isinstance(raw, dict):
                                continue
                            local_sensor_preferences[key] = {
                                "mqtt_enabled": bool(raw.get("mqtt_enabled", True)),
                            }
                except (TypeError, ValueError, json.JSONDecodeError):
                    local_sensor_preferences = None
            devices.append(
                DeviceConfig(
                    device_uid=row["device_uid"],
                    id=row["id"],
                    source=row["source"],
                    host=row["host"],
                    port=row["port"],
                    unit_id=row["unit_id"],
                    snmp_community=row["snmp_community"],
                    poll_interval=row["poll_interval"],
                    name=row["name"],
                    debug_logging=bool(row["debug_logging"]),
                    keep_connection_open=bool(row["keep_connection_open"]),
                    discovery_enabled=bool(row["discovery_enabled"]),
                    polling_enabled=bool(row["polling_enabled"]),
                    profile_uid=str(row["profile_uid"] or ""),
                    profile_mode=str(row["profile_mode"] or "local"),
                    local_profile_payload=local_profile_payload,
                    local_selected_sensors=local_selected_sensors,
                    local_sensor_preferences=local_sensor_preferences,
                )
            )
        return devices

    @staticmethod
    def _ensure_column(
        cursor: sqlite3.Cursor, table: str, column: str, definition: str
    ) -> None:
        """Add a column to an existing table if it does not exist."""
        if table == "devices":
            migration_sql = _DEVICE_COLUMN_MIGRATIONS.get(column)
        elif table == "profiles":
            migration_sql = _PROFILE_COLUMN_MIGRATIONS.get(column)
        else:
            raise ValueError(f"Unsupported table for migration: {table}")
        if migration_sql is None:
            raise ValueError(f"Unsupported column for migration: {column}")
        if definition not in migration_sql:
            raise ValueError(f"Unexpected definition for {column}: {definition}")
        if table == "devices":
            existing = cursor.execute("PRAGMA table_info(devices)").fetchall()
        else:
            existing = cursor.execute("PRAGMA table_info(profiles)").fetchall()
        names = {str(row[1]) for row in existing}
        if column in names:
            return
        cursor.execute(migration_sql)

    def delete_device(self, device_uid: str) -> bool:
        """Delete a device by device_uid."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM devices WHERE device_uid = ?", (device_uid,))
        deleted = cursor.rowcount > 0
        conn.commit()
        return deleted

    def cleanup_state(self, valid_device_uids: set[str]) -> dict[str, int]:
        """Remove stale rows from devices table.

        Args:
            valid_device_uids: Active immutable device UIDs that should remain.
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        if valid_device_uids:
            placeholders = ",".join("?" for _ in valid_device_uids)
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            cursor.execute(
                f"DELETE FROM devices WHERE device_uid NOT IN ({placeholders})",
                tuple(sorted(valid_device_uids)),
            )
        else:
            cursor.execute("DELETE FROM devices")
        devices_removed = max(0, int(cursor.rowcount))

        conn.commit()
        return {
            "devices_removed": devices_removed,
        }

    def save_profile(self, profile: ProfileConfig) -> None:
        """Save or update a profile."""
        conn = self._get_conn()
        cursor = conn.cursor()
        # Honor DISABLE_PROFILE_PROTECTION env var for development
        if self._is_profile_protection_disabled():
            protected = bool(profile.is_protected)
        else:
            protected = bool(profile.is_protected) or (
                (profile.name, profile.driver_key) in DEFAULT_PROTECTED_PROFILES
            )
        cursor.execute(
            """
            INSERT INTO profiles (
                profile_uid, name, driver_key, config_payload, selected_sensors, comments, is_protected, updated_at
                , sensor_preferences
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(profile_uid) DO UPDATE SET
                name = excluded.name,
                driver_key = excluded.driver_key,
                config_payload = excluded.config_payload,
                selected_sensors = excluded.selected_sensors,
                comments = excluded.comments,
                is_protected = excluded.is_protected,
                sensor_preferences = excluded.sensor_preferences,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                profile.profile_uid,
                profile.name,
                profile.driver_key,
                json.dumps(profile.config_payload, sort_keys=True),
                json.dumps(profile.selected_sensors),
                profile.comments or "",
                1 if protected else 0,
                json.dumps(profile.sensor_preferences, sort_keys=True)
                if isinstance(profile.sensor_preferences, dict)
                else None,
            ),
        )
        conn.commit()

    def load_profiles(self) -> list[ProfileConfig]:
        """Load all profiles from database."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM profiles ORDER BY created_at, name")
        rows = cursor.fetchall()
        items: list[ProfileConfig] = []
        for row in rows:
            config_payload = json.loads(str(row["config_payload"]) or "{}")
            if not isinstance(config_payload, dict):
                config_payload = {}
            selected_sensors = json.loads(str(row["selected_sensors"]) or "[]")
            if not isinstance(selected_sensors, list):
                selected_sensors = []
            sensor_preferences: dict[str, dict[str, bool]] | None = None
            if row["sensor_preferences"]:
                loaded_preferences = json.loads(str(row["sensor_preferences"]) or "{}")
                if isinstance(loaded_preferences, dict):
                    sensor_preferences = {}
                    for key, raw in loaded_preferences.items():
                        if not isinstance(key, str) or not isinstance(raw, dict):
                            continue
                        sensor_preferences[key] = {
                            "mqtt_enabled": bool(raw.get("mqtt_enabled", True)),
                        }
            items.append(
                ProfileConfig(
                    profile_uid=str(row["profile_uid"]),
                    name=str(row["name"]),
                    driver_key=str(row["driver_key"]),
                    config_payload=config_payload,
                    selected_sensors=[str(item) for item in selected_sensors],
                    sensor_preferences=sensor_preferences,
                    comments=str(row["comments"] or ""),
                    is_protected=bool(row["is_protected"]),
                )
            )
        return items

    def delete_profile(self, profile_uid: str) -> bool:
        """Delete a profile by profile_uid."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM profiles WHERE profile_uid = ?", (profile_uid,))
        deleted = cursor.rowcount > 0
        conn.commit()
        return deleted

    def close(self) -> None:
        """Close database connections."""
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            del self._local.conn

    @staticmethod
    def _is_profile_protection_disabled() -> bool:
        """Check if profile protection is disabled via environment variable."""
        return os.environ.get("DISABLE_PROFILE_PROTECTION", "").lower() in {
            "1",
            "true",
            "yes",
        }

    def _mark_default_profiles_protected(self, cursor: sqlite3.Cursor) -> None:
        """Mark known system default profiles as protected in-place."""
        for name, driver_key in DEFAULT_PROTECTED_PROFILES:
            cursor.execute(
                """
                UPDATE profiles
                SET is_protected = 1
                WHERE name = ? AND driver_key = ?
                """,
                (name, driver_key),
            )
