# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from copy import deepcopy
from typing import Any

from .database import Database
from .drivers.registry import DRIVER_REGISTRY
from .drivers.runtime_metadata import (
    load_plugin_capability_profile,
    load_plugin_sensor_catalog,
)

_BASELINE_SEED_KEY = "baseline_v1"

_REPOSITORY: "CapabilityRepository | None" = None

_ENUM_SENSOR_VALUE_MAPS: dict[str, dict[str, str]] = {
    "output_source": {
        "1": "other",
        "2": "none",
        "3": "normal",
        "4": "bypass",
        "5": "battery",
        "6": "booster",
        "7": "reducer",
    },
    "battery_status": {
        "1": "unknown",
        "2": "battery_normal",
        "3": "battery_low",
        "4": "battery_depleted",
    },
}

_BITFIELD_FLAG_MAPS: dict[str, dict[int, tuple[str, str]]] = {
    "ups_status_bf": {
        0: ("ups_online_state", "UPS Online"),
        1: ("ups_on_battery_state", "UPS On Battery"),
        2: ("ups_on_bypass_state", "UPS On Bypass"),
        3: ("ups_output_off_state", "UPS Output Off"),
    },
    "battery_system_error_bf": {
        0: ("ups_low_battery_state", "UPS Low Battery"),
    },
}


class CapabilityRepository:
    """Database-backed capability metadata source of truth."""

    def __init__(self, db: Database):
        self._db = db

    def seed_baseline_if_needed(self) -> None:
        conn = self._db._get_conn()
        cursor = conn.cursor()

        payload = self._build_seed_payload()
        payload_json = _stable_json(payload)
        seed_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

        row = cursor.execute(
            "SELECT seed_hash FROM capability_seed_version WHERE seed_key = ?",
            (_BASELINE_SEED_KEY,),
        ).fetchone()
        if row and str(row["seed_hash"] or "") == seed_hash:
            return

        cursor.execute("BEGIN")
        try:
            self._clear_seed_tables(cursor)
            self._seed_transports(cursor)
            self._seed_metric_contracts(cursor, payload.get("metric_contracts", {}))
            self._seed_drivers(cursor, payload.get("drivers", {}))
            cursor.execute(
                """
                INSERT INTO capability_seed_version(seed_key, seed_hash, seeded_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(seed_key) DO UPDATE SET
                    seed_hash = excluded.seed_hash,
                    seeded_at = CURRENT_TIMESTAMP
                """,
                (_BASELINE_SEED_KEY, seed_hash),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def load_runtime_profiles(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            SELECT p.driver_key, p.profile_json
            FROM capability_driver_profiles p
            INNER JOIN capability_drivers d ON d.driver_key = p.driver_key
            WHERE d.enabled = 1
            ORDER BY p.driver_key
            """
        ).fetchall()

        profiles: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for row in rows:
            driver_key = str(row["driver_key"])
            try:
                profile = json.loads(str(row["profile_json"]))
                if not isinstance(profile, dict):
                    raise ValueError("profile_json must decode to object")
            except (TypeError, ValueError, json.JSONDecodeError) as err:
                errors.append(f"{driver_key}: invalid profile_json ({err})")
                continue
            merged = self._apply_overrides(driver_key, profile)
            profiles[driver_key] = merged
        return profiles, errors

    def load_catalog_sensor_rows(self, driver_key: str) -> list[dict[str, str]]:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            SELECT sensor_key, label, category, unit, source, aliases_json, reference, tier
            FROM capability_sensors
            WHERE driver_key = ?
            ORDER BY position, sensor_key
            """,
            (driver_key,),
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            aliases = _json_list(str(row["aliases_json"] or "[]"))
            result.append(
                {
                    "key": str(row["sensor_key"]),
                    "label": str(row["label"] or str(row["sensor_key"])),
                    "category": str(row["category"] or "other"),
                    "unit": str(row["unit"] or ""),
                    "source": str(row["source"] or ""),
                    "aliases": ", ".join(aliases),
                    "reference": str(row["reference"] or ""),
                    "tier": str(row["tier"] or "normalized"),
                }
            )

        overrides = cursor.execute(
            """
            SELECT sensor_key, override_json, is_deleted
            FROM capability_sensor_overrides
            WHERE driver_key = ?
            ORDER BY sensor_key
            """,
            (driver_key,),
        ).fetchall()

        keyed: dict[str, dict[str, Any]] = {str(item["key"]): dict(item) for item in result}
        for row in overrides:
            sensor_key = str(row["sensor_key"])
            if int(row["is_deleted"] or 0):
                keyed.pop(sensor_key, None)
                continue
            try:
                payload = json.loads(str(row["override_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            base = keyed.setdefault(
                sensor_key,
                {
                    "key": sensor_key,
                    "label": sensor_key,
                    "category": "other",
                    "unit": "",
                    "source": "",
                    "aliases": "",
                    "reference": "",
                    "tier": "normalized",
                },
            )
            for field in (
                "label",
                "category",
                "unit",
                "source",
                "aliases",
                "reference",
                "tier",
            ):
                if field in payload:
                    base[field] = str(payload[field])

        enum_maps = self._load_merged_value_maps(driver_key)
        for sensor_key in sorted(enum_maps.keys()):
            suffix = "_text"
            companion_key = f"{sensor_key}{suffix}"
            if companion_key in keyed:
                continue
            base_label = keyed.get(sensor_key, {}).get("label", sensor_key)
            keyed[companion_key] = {
                "key": companion_key,
                "label": f"{base_label} Text",
                "category": "status",
                "unit": "",
                "source": "derived",
                "aliases": "",
                "reference": sensor_key,
                "tier": keyed.get(sensor_key, {}).get("tier", "normalized"),
            }

        bit_rows = self._load_merged_bitfield_flags(driver_key)
        for row in bit_rows:
            flag_key = str(row["flag_key"])
            if flag_key in keyed:
                continue
            keyed[flag_key] = {
                "key": flag_key,
                "label": str(row["label"] or flag_key),
                "category": str(row["category"] or "status"),
                "unit": "",
                "source": "derived",
                "aliases": "",
                "reference": f"{row['source_key']}:bit{int(row['bit_index'])}",
                "tier": str(row["tier"] or "extended"),
            }

        return [dict(item) for item in keyed.values()]

    def load_catalog_derived_metrics(self, driver_key: str) -> list[dict[str, Any]]:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            SELECT spec_json
            FROM capability_derived_metrics
            WHERE driver_key = ?
            ORDER BY metric_key
            """,
            (driver_key,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["spec_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                out.append(payload)

        enum_maps = self._load_merged_value_maps(driver_key)
        for sensor_key, mapping in enum_maps.items():
            if not mapping:
                continue
            out.append(
                {
                    "output_key": f"{sensor_key}_text",
                    "source_key": sensor_key,
                    "transform": "enum_map",
                    "output_type": "string",
                    "null_to_value_fill": True,
                    "params": {"map": mapping},
                }
            )

        bit_rows = self._load_merged_bitfield_flags(driver_key)
        for row in bit_rows:
            out.append(
                {
                    "output_key": str(row["flag_key"]),
                    "source_key": str(row["source_key"]),
                    "transform": "bitfield_bit_to_bool",
                    "output_type": "bool",
                    "null_to_value_fill": True,
                    "params": {"bit": int(row["bit_index"])},
                }
            )
        return out

    def load_bitfield_source_keys(self, driver_key: str) -> set[str]:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            SELECT sensor_key
            FROM capability_sensors
            WHERE driver_key = ? AND lower(sensor_key) LIKE '%_bf'
            """,
            (driver_key,),
        ).fetchall()
        return {str(row["sensor_key"]).strip() for row in rows if str(row["sensor_key"]).strip()}

    def _load_merged_value_maps(self, driver_key: str) -> dict[str, dict[str, str]]:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            SELECT sensor_key, raw_value, display_text
            FROM capability_value_maps
            WHERE driver_key = ?
            ORDER BY sensor_key, raw_value
            """,
            (driver_key,),
        ).fetchall()
        out: dict[str, dict[str, str]] = {}
        for row in rows:
            sensor_key = str(row["sensor_key"])
            out.setdefault(sensor_key, {})[str(row["raw_value"])] = str(
                row["display_text"]
            )

        overrides = cursor.execute(
            """
            SELECT sensor_key, raw_value, display_text, is_deleted
            FROM capability_value_map_overrides
            WHERE driver_key = ?
            ORDER BY sensor_key, raw_value
            """,
            (driver_key,),
        ).fetchall()
        for row in overrides:
            sensor_key = str(row["sensor_key"])
            raw_value = str(row["raw_value"])
            if int(row["is_deleted"] or 0):
                sensor_map = out.get(sensor_key, {})
                sensor_map.pop(raw_value, None)
                continue
            out.setdefault(sensor_key, {})[raw_value] = str(row["display_text"])

        return {k: v for k, v in out.items() if v}

    def _load_merged_bitfield_flags(self, driver_key: str) -> list[dict[str, Any]]:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            SELECT source_key, bit_index, flag_key, label, category, tier
            FROM capability_bitfield_flags
            WHERE driver_key = ?
            ORDER BY source_key, bit_index, flag_key
            """,
            (driver_key,),
        ).fetchall()
        keyed: dict[tuple[str, int, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row["source_key"]), int(row["bit_index"]), str(row["flag_key"]))
            keyed[key] = {
                "source_key": key[0],
                "bit_index": key[1],
                "flag_key": key[2],
                "label": str(row["label"]),
                "category": str(row["category"] or "status"),
                "tier": str(row["tier"] or "extended"),
            }

        overrides = cursor.execute(
            """
            SELECT source_key, bit_index, flag_key, label, category, tier, is_deleted
            FROM capability_bitfield_flag_overrides
            WHERE driver_key = ?
            ORDER BY source_key, bit_index, flag_key
            """,
            (driver_key,),
        ).fetchall()
        for row in overrides:
            key = (str(row["source_key"]), int(row["bit_index"]), str(row["flag_key"]))
            if int(row["is_deleted"] or 0):
                keyed.pop(key, None)
                continue
            keyed[key] = {
                "source_key": key[0],
                "bit_index": key[1],
                "flag_key": key[2],
                "label": str(row["label"] or key[2]),
                "category": str(row["category"] or "status"),
                "tier": str(row["tier"] or "extended"),
            }

        return [keyed[key] for key in sorted(keyed.keys())]

    def load_metric_contracts(self) -> dict[str, dict[str, Any]]:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            SELECT metric_key, value_kind, canonical_unit, default_multiplier, default_offset, default_unit
            FROM capability_metric_contracts
            ORDER BY metric_key
            """
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["metric_key"])
            out[key] = {
                "value_kind": str(row["value_kind"] or "str"),
                "canonical_unit": str(row["canonical_unit"] or ""),
                "default_normalization": {
                    "multiplier": float(row["default_multiplier"] or 1.0),
                    "offset": float(row["default_offset"] or 0.0),
                    "unit": str(row["default_unit"] or ""),
                },
            }
        return out

    def upsert_sensor_override(
        self,
        *,
        driver_key: str,
        sensor_key: str,
        override: dict[str, Any] | None,
        is_deleted: bool = False,
    ) -> None:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO capability_sensor_overrides(
                driver_key, sensor_key, override_json, is_deleted, updated_at
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(driver_key, sensor_key) DO UPDATE SET
                override_json = excluded.override_json,
                is_deleted = excluded.is_deleted,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                driver_key,
                sensor_key,
                _stable_json(override or {}),
                1 if is_deleted else 0,
            ),
        )
        conn.commit()

    def upsert_key_precedence_override(
        self,
        *,
        driver_key: str,
        sensor_key: str,
        preferred_source: str,
        is_deleted: bool = False,
    ) -> None:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO capability_key_precedence_overrides(
                driver_key, sensor_key, preferred_source, is_deleted, updated_at
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(driver_key, sensor_key) DO UPDATE SET
                preferred_source = excluded.preferred_source,
                is_deleted = excluded.is_deleted,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                driver_key,
                sensor_key,
                preferred_source,
                1 if is_deleted else 0,
            ),
        )
        conn.commit()

    def upsert_mapping_override(
        self,
        *,
        driver_key: str,
        transport_name: str,
        mapping_kind: str,
        sensor_key: str,
        match_value: str,
        override: dict[str, Any] | None,
        is_deleted: bool = False,
    ) -> None:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO capability_mapping_overrides(
                driver_key, transport_name, mapping_kind, sensor_key, match_value,
                override_json, is_deleted, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(driver_key, transport_name, mapping_kind, sensor_key, match_value)
            DO UPDATE SET
                override_json = excluded.override_json,
                is_deleted = excluded.is_deleted,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                driver_key,
                transport_name,
                mapping_kind,
                sensor_key,
                match_value,
                _stable_json(override or {}),
                1 if is_deleted else 0,
            ),
        )
        conn.commit()

    def upsert_value_map_override(
        self,
        *,
        driver_key: str,
        sensor_key: str,
        raw_value: str,
        display_text: str,
        is_deleted: bool = False,
    ) -> None:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO capability_value_map_overrides(
                driver_key, sensor_key, raw_value, display_text, is_deleted, updated_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(driver_key, sensor_key, raw_value) DO UPDATE SET
                display_text = excluded.display_text,
                is_deleted = excluded.is_deleted,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                driver_key,
                sensor_key,
                raw_value,
                display_text,
                1 if is_deleted else 0,
            ),
        )
        conn.commit()

    def upsert_bitfield_flag_override(
        self,
        *,
        driver_key: str,
        source_key: str,
        bit_index: int,
        flag_key: str,
        label: str,
        category: str = "status",
        tier: str = "extended",
        is_deleted: bool = False,
    ) -> None:
        conn = self._db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO capability_bitfield_flag_overrides(
                driver_key, source_key, bit_index, flag_key, label, category, tier, is_deleted, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(driver_key, source_key, bit_index, flag_key) DO UPDATE SET
                label = excluded.label,
                category = excluded.category,
                tier = excluded.tier,
                is_deleted = excluded.is_deleted,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                driver_key,
                source_key,
                int(bit_index),
                flag_key,
                label,
                category,
                tier,
                1 if is_deleted else 0,
            ),
        )
        conn.commit()

    def _apply_overrides(self, driver_key: str, profile: dict[str, Any]) -> dict[str, Any]:
        out = deepcopy(profile)
        conn = self._db._get_conn()
        cursor = conn.cursor()

        precedence_rows = cursor.execute(
            """
            SELECT sensor_key, preferred_source, is_deleted
            FROM capability_key_precedence_overrides
            WHERE driver_key = ?
            """,
            (driver_key,),
        ).fetchall()
        if precedence_rows:
            precedence = out.get("key_precedence")
            if not isinstance(precedence, dict):
                precedence = {}
                out["key_precedence"] = precedence
            for row in precedence_rows:
                key = str(row["sensor_key"])
                if int(row["is_deleted"] or 0):
                    precedence.pop(key, None)
                else:
                    precedence[key] = str(row["preferred_source"])

        mapping_rows = cursor.execute(
            """
            SELECT transport_name, mapping_kind, sensor_key, match_value, override_json, is_deleted
            FROM capability_mapping_overrides
            WHERE driver_key = ?
            ORDER BY transport_name, mapping_kind, sensor_key
            """,
            (driver_key,),
        ).fetchall()
        if mapping_rows:
            for row in mapping_rows:
                transport_name = str(row["transport_name"])
                mapping_kind = str(row["mapping_kind"])
                sensor_key = str(row["sensor_key"])
                match_value = str(row["match_value"])
                try:
                    override_payload = json.loads(str(row["override_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    override_payload = {}
                if not isinstance(override_payload, dict):
                    override_payload = {}

                is_deleted = int(row["is_deleted"] or 0) == 1
                self._apply_mapping_override(
                    out,
                    transport_name=transport_name,
                    mapping_kind=mapping_kind,
                    sensor_key=sensor_key,
                    match_value=match_value,
                    override=override_payload,
                    is_deleted=is_deleted,
                )

        return out

    @staticmethod
    def _apply_mapping_override(
        profile: dict[str, Any],
        *,
        transport_name: str,
        mapping_kind: str,
        sensor_key: str,
        match_value: str,
        override: dict[str, Any],
        is_deleted: bool,
    ) -> None:
        candidates: list[Any] = []

        protocol = str(profile.get("protocol", ""))
        if protocol == "modbus":
            candidates = profile.get("registers", []) if mapping_kind == "modbus" else []
        elif protocol == "snmp":
            candidates = profile.get("oids", {}) if mapping_kind == "snmp" else {}
        elif protocol == "hybrid":
            if mapping_kind == "modbus":
                candidates = profile.get("modbus", {}).get("registers", [])
            elif mapping_kind == "snmp":
                candidates = profile.get("snmp", {}).get("oids", {})
        elif protocol == "multi_source":
            src = profile.get("active_sources", {}).get(transport_name, {})
            if mapping_kind == "modbus":
                candidates = src.get("registers", [])
            elif mapping_kind == "snmp":
                candidates = src.get("oids", {})

        if isinstance(candidates, dict):
            spec = candidates.get(sensor_key)
            if not isinstance(spec, dict):
                return
            if is_deleted:
                candidates.pop(sensor_key, None)
                return
            spec.update(override)
            return

        if not isinstance(candidates, list):
            return

        match_address: int | None = None
        try:
            match_address = int(match_value)
        except ValueError:
            match_address = None

        for idx, item in enumerate(list(candidates)):
            if not isinstance(item, dict):
                continue
            if str(item.get("key", "")) != sensor_key:
                continue
            address = item.get("address")
            if match_address is not None and address is not None:
                try:
                    if int(address) != match_address:
                        continue
                except (TypeError, ValueError):
                    continue
            if is_deleted:
                candidates.pop(idx)
                return
            item.update(override)
            return

    @staticmethod
    def _clear_seed_tables(cursor: sqlite3.Cursor) -> None:
        cursor.executescript(
            """
            DELETE FROM capability_driver_transports;
            DELETE FROM capability_driver_profiles;
            DELETE FROM capability_poll_groups;
            DELETE FROM capability_modbus_mappings;
            DELETE FROM capability_snmp_mappings;
            DELETE FROM capability_bacnet_mappings;
            DELETE FROM capability_rest_mappings;
            DELETE FROM capability_register_blocks;
            DELETE FROM capability_snmp_blocks;
            DELETE FROM capability_key_precedence;
            DELETE FROM capability_sensors;
            DELETE FROM capability_derived_metrics;
            DELETE FROM capability_value_maps;
            DELETE FROM capability_bitfield_flags;
            DELETE FROM capability_metric_contracts;
            DELETE FROM capability_drivers;
            DELETE FROM capability_transports;
            """
        )

    @staticmethod
    def _seed_transports(cursor: sqlite3.Cursor) -> None:
        transport_rows = [
            ("modbus", 1),
            ("snmp", 1),
            ("hybrid", 1),
            ("multi_source", 1),
            ("nut", 1),
            ("bacnet", 0),
            ("rest", 0),
        ]
        for name, supported in transport_rows:
            cursor.execute(
                """
                INSERT INTO capability_transports(name, runtime_supported)
                VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET runtime_supported = excluded.runtime_supported
                """,
                (name, supported),
            )

    def _seed_metric_contracts(
        self, cursor: sqlite3.Cursor, contracts: dict[str, dict[str, Any]]
    ) -> None:
        for key, spec in contracts.items():
            if not isinstance(spec, dict):
                continue
            default = spec.get("default_normalization", {})
            if not isinstance(default, dict):
                default = {}
            cursor.execute(
                """
                INSERT INTO capability_metric_contracts(
                    metric_key, value_kind, canonical_unit,
                    default_multiplier, default_offset, default_unit
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(key),
                    str(spec.get("value_kind", "str")),
                    str(spec.get("canonical_unit", "")),
                    float(default.get("multiplier", 1.0)),
                    float(default.get("offset", 0.0)),
                    str(default.get("unit", "")),
                ),
            )

    def _seed_drivers(self, cursor: sqlite3.Cursor, drivers: dict[str, dict[str, Any]]) -> None:
        for driver_key, payload in sorted(drivers.items()):
            descriptor = DRIVER_REGISTRY.get(driver_key)
            profile = payload.get("profile")
            catalog = payload.get("catalog")
            if not isinstance(profile, dict):
                continue
            if not isinstance(catalog, dict):
                catalog = {}

            protocol = str(profile.get("protocol") or (descriptor.transport if descriptor else ""))
            cursor.execute(
                """
                INSERT INTO capability_drivers(
                    driver_key, family, protocol, transport, owns_runtime_metadata,
                    enabled, display_name, vendor_display, family_display,
                    source_display, search_aliases_json
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    driver_key,
                    str(descriptor.family if descriptor else driver_key),
                    protocol,
                    str(descriptor.transport if descriptor else protocol),
                    1 if (descriptor and descriptor.owns_runtime_metadata) else 0,
                    str(descriptor.display_name or "") if descriptor else "",
                    str(descriptor.vendor_display or "") if descriptor else "",
                    str(descriptor.family_display or "") if descriptor else "",
                    str(descriptor.source_display or "") if descriptor else "",
                    _stable_json(list(descriptor.search_aliases or []) if descriptor else []),
                ),
            )
            cursor.execute(
                """
                INSERT INTO capability_driver_profiles(driver_key, profile_json, profile_hash, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    driver_key,
                    _stable_json(profile),
                    hashlib.sha256(_stable_json(profile).encode("utf-8")).hexdigest(),
                ),
            )

            for transport_name in _transports_for_profile(profile, descriptor):
                cursor.execute(
                    "INSERT OR IGNORE INTO capability_driver_transports(driver_key, transport_name) VALUES (?, ?)",
                    (driver_key, transport_name),
                )

            poll_groups = profile.get("poll_groups", {})
            if isinstance(poll_groups, dict):
                for group_name, group_spec in poll_groups.items():
                    if not isinstance(group_spec, dict):
                        continue
                    try:
                        interval_s = int(group_spec.get("interval_s", 60))
                    except (TypeError, ValueError):
                        interval_s = 60
                    cursor.execute(
                        """
                        INSERT INTO capability_poll_groups(driver_key, group_name, interval_s)
                        VALUES (?, ?, ?)
                        """,
                        (driver_key, str(group_name), max(1, interval_s)),
                    )

            self._seed_profile_mappings(cursor, driver_key=driver_key, profile=profile)
            self._seed_catalog(cursor, driver_key=driver_key, catalog=catalog)

    def _seed_profile_mappings(
        self,
        cursor: sqlite3.Cursor,
        *,
        driver_key: str,
        profile: dict[str, Any],
    ) -> None:
        protocol = str(profile.get("protocol", ""))

        if protocol == "modbus":
            self._insert_modbus_mappings(
                cursor,
                driver_key=driver_key,
                transport_name="modbus",
                registers=profile.get("registers", []),
            )
            self._insert_register_blocks(
                cursor,
                driver_key=driver_key,
                transport_name="modbus",
                blocks=profile.get("register_blocks", []),
            )
        elif protocol == "snmp":
            self._insert_snmp_mappings(
                cursor,
                driver_key=driver_key,
                transport_name="snmp",
                oids=profile.get("oids", {}),
            )
            self._insert_snmp_blocks(
                cursor,
                driver_key=driver_key,
                transport_name="snmp",
                blocks=profile.get("snmp_blocks", []),
            )
        elif protocol == "hybrid":
            modbus = profile.get("modbus", {})
            snmp = profile.get("snmp", {})
            if isinstance(modbus, dict):
                self._insert_modbus_mappings(
                    cursor,
                    driver_key=driver_key,
                    transport_name="modbus",
                    registers=modbus.get("registers", []),
                )
                self._insert_register_blocks(
                    cursor,
                    driver_key=driver_key,
                    transport_name="modbus",
                    blocks=modbus.get("register_blocks", []),
                )
            if isinstance(snmp, dict):
                self._insert_snmp_mappings(
                    cursor,
                    driver_key=driver_key,
                    transport_name="snmp",
                    oids=snmp.get("oids", {}),
                )
                self._insert_snmp_blocks(
                    cursor,
                    driver_key=driver_key,
                    transport_name="snmp",
                    blocks=snmp.get("snmp_blocks", []),
                )
            precedence = profile.get("key_precedence", {})
            if isinstance(precedence, dict):
                for key, preferred in precedence.items():
                    cursor.execute(
                        """
                        INSERT INTO capability_key_precedence(driver_key, sensor_key, preferred_source)
                        VALUES (?, ?, ?)
                        """,
                        (driver_key, str(key), str(preferred)),
                    )
        elif protocol == "multi_source":
            active_sources = profile.get("active_sources", {})
            if isinstance(active_sources, dict):
                for source_name in ("modbus", "snmp"):
                    source_spec = active_sources.get(source_name)
                    if not isinstance(source_spec, dict):
                        continue
                    if source_name == "modbus":
                        self._insert_modbus_mappings(
                            cursor,
                            driver_key=driver_key,
                            transport_name="modbus",
                            registers=source_spec.get("registers", []),
                        )
                        self._insert_register_blocks(
                            cursor,
                            driver_key=driver_key,
                            transport_name="modbus",
                            blocks=source_spec.get("register_blocks", []),
                        )
                    if source_name == "snmp":
                        self._insert_snmp_mappings(
                            cursor,
                            driver_key=driver_key,
                            transport_name="snmp",
                            oids=source_spec.get("oids", {}),
                        )
                        self._insert_snmp_blocks(
                            cursor,
                            driver_key=driver_key,
                            transport_name="snmp",
                            blocks=source_spec.get("snmp_blocks", []),
                        )

    @staticmethod
    def _insert_modbus_mappings(
        cursor: sqlite3.Cursor,
        *,
        driver_key: str,
        transport_name: str,
        registers: Any,
    ) -> None:
        if not isinstance(registers, list):
            return
        for item in registers:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            if not key:
                continue
            address = item.get("address")
            if address is None:
                continue
            try:
                address_int = int(address)
            except (TypeError, ValueError):
                continue
            count = int(item.get("count", 1) or 1)
            cursor.execute(
                """
                INSERT INTO capability_modbus_mappings(
                    driver_key, transport_name, sensor_key, address, count,
                    data_type, scale, word_order, poll_group, spec_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    driver_key,
                    transport_name,
                    key,
                    address_int,
                    count,
                    str(item.get("type", "uint16")),
                    float(item.get("scale", 1) or 1),
                    str(item.get("word_order", "big")),
                    str(item.get("poll_group", "slow")),
                    _stable_json(item),
                ),
            )

    @staticmethod
    def _insert_snmp_mappings(
        cursor: sqlite3.Cursor,
        *,
        driver_key: str,
        transport_name: str,
        oids: Any,
    ) -> None:
        if not isinstance(oids, dict):
            return
        for key, spec in oids.items():
            if not isinstance(spec, dict):
                continue
            oid = str(spec.get("oid", "")).strip()
            if not oid:
                continue
            cursor.execute(
                """
                INSERT INTO capability_snmp_mappings(
                    driver_key, transport_name, sensor_key, oid, poll_group,
                    timeticks_minutes, spec_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    driver_key,
                    transport_name,
                    str(key),
                    oid,
                    str(spec.get("poll_group", "slow")),
                    1 if bool(spec.get("timeticks_minutes", False)) else 0,
                    _stable_json(spec),
                ),
            )

    @staticmethod
    def _insert_register_blocks(
        cursor: sqlite3.Cursor,
        *,
        driver_key: str,
        transport_name: str,
        blocks: Any,
    ) -> None:
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            name = str(block.get("name", "")).strip() or "block"
            try:
                start = int(block.get("start_address"))
                count = int(block.get("count"))
            except (TypeError, ValueError):
                continue
            cursor.execute(
                """
                INSERT INTO capability_register_blocks(
                    driver_key, transport_name, name, start_address, count,
                    poll_group, spec_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    driver_key,
                    transport_name,
                    name,
                    start,
                    count,
                    str(block.get("poll_group", "slow")),
                    _stable_json(block),
                ),
            )

    @staticmethod
    def _insert_snmp_blocks(
        cursor: sqlite3.Cursor,
        *,
        driver_key: str,
        transport_name: str,
        blocks: Any,
    ) -> None:
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            name = str(block.get("name", "")).strip() or "snmp_block"
            metrics = block.get("metrics", [])
            metrics_json = _stable_json(metrics if isinstance(metrics, list) else [])
            cursor.execute(
                """
                INSERT INTO capability_snmp_blocks(
                    driver_key, transport_name, name, poll_group, metrics_json, spec_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    driver_key,
                    transport_name,
                    name,
                    str(block.get("poll_group", "slow")),
                    metrics_json,
                    _stable_json(block),
                ),
            )

    @staticmethod
    def _seed_catalog(
        cursor: sqlite3.Cursor,
        *,
        driver_key: str,
        catalog: dict[str, Any],
    ) -> None:
        sensors = catalog.get("sensors", []) if isinstance(catalog, dict) else []
        if isinstance(sensors, list):
            for index, item in enumerate(sensors):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", "")).strip()
                if not key:
                    continue
                aliases = item.get("aliases", [])
                aliases_json = _stable_json(
                    [str(alias) for alias in aliases if str(alias)]
                    if isinstance(aliases, list)
                    else []
                )
                reference = ""
                if "reference" in item:
                    reference = str(item.get("reference", ""))
                elif "register" in item:
                    reference = str(item.get("register", ""))
                elif "oid" in item:
                    reference = str(item.get("oid", ""))
                elif "url" in item:
                    reference = str(item.get("url", ""))

                cursor.execute(
                    """
                    INSERT INTO capability_sensors(
                        driver_key, sensor_key, label, category, unit, source,
                        aliases_json, reference, tier, note, position, spec_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        driver_key,
                        key,
                        str(item.get("label", key)),
                        str(item.get("category", "other")),
                        str(item.get("unit", "")),
                        str(item.get("source", "")),
                        aliases_json,
                        reference,
                        str(item.get("tier", "normalized")),
                        str(item.get("note", "")),
                        index,
                        _stable_json(item),
                    ),
                )
                CapabilityRepository._seed_humanization_mappings(
                    cursor=cursor,
                    driver_key=driver_key,
                    sensor_key=key,
                )

        derived_metrics = (
            catalog.get("derived_metrics", []) if isinstance(catalog, dict) else []
        )
        if isinstance(derived_metrics, list):
            for index, item in enumerate(derived_metrics):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", f"derived_{index}"))
                cursor.execute(
                    """
                    INSERT INTO capability_derived_metrics(driver_key, metric_key, spec_json)
                    VALUES (?, ?, ?)
                    """,
                    (driver_key, key, _stable_json(item)),
                )

    @staticmethod
    def _seed_humanization_mappings(
        *,
        cursor: sqlite3.Cursor,
        driver_key: str,
        sensor_key: str,
    ) -> None:
        enum_map = _ENUM_SENSOR_VALUE_MAPS.get(sensor_key)
        if isinstance(enum_map, dict):
            for raw_value, display_text in enum_map.items():
                cursor.execute(
                    """
                    INSERT INTO capability_value_maps(
                        driver_key, sensor_key, raw_value, display_text, publish_raw, text_suffix
                    ) VALUES (?, ?, ?, ?, 1, '_text')
                    """,
                    (driver_key, sensor_key, str(raw_value), str(display_text)),
                )

        bit_map = _BITFIELD_FLAG_MAPS.get(sensor_key)
        if isinstance(bit_map, dict):
            for bit_index, declaration in bit_map.items():
                flag_key, label = declaration
                cursor.execute(
                    """
                    INSERT INTO capability_bitfield_flags(
                        driver_key, source_key, bit_index, flag_key, label, category, tier
                    ) VALUES (?, ?, ?, ?, ?, 'status', 'extended')
                    """,
                    (
                        driver_key,
                        sensor_key,
                        int(bit_index),
                        str(flag_key),
                        str(label),
                    ),
                )

    @staticmethod
    def _build_seed_payload() -> dict[str, Any]:
        drivers_payload: dict[str, dict[str, Any]] = {}
        for driver_key in sorted(DRIVER_REGISTRY.keys()):
            profile: dict[str, Any] | None = None
            catalog: dict[str, Any] | None = None
            try:
                loaded_profile = load_plugin_capability_profile(driver_key)
                if isinstance(loaded_profile, dict):
                    profile = loaded_profile
            except (
                ImportError,
                AttributeError,
                NotImplementedError,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                profile = None
            try:
                loaded_catalog = load_plugin_sensor_catalog(driver_key)
                if isinstance(loaded_catalog, dict):
                    catalog = loaded_catalog
            except (
                ImportError,
                AttributeError,
                NotImplementedError,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                catalog = None
            if profile is None:
                continue
            drivers_payload[driver_key] = {
                "profile": profile,
                "catalog": catalog or {},
            }

        metric_contracts = _load_metric_contracts_from_bundle()
        return {
            "drivers": drivers_payload,
            "metric_contracts": metric_contracts,
            "humanization_maps": {
                "enum": _ENUM_SENSOR_VALUE_MAPS,
                "bitfield": _BITFIELD_FLAG_MAPS,
            },
        }



def configure_capability_repository(db: Database) -> CapabilityRepository:
    global _REPOSITORY
    _REPOSITORY = CapabilityRepository(db)
    return _REPOSITORY


def get_capability_repository() -> CapabilityRepository:
    global _REPOSITORY
    if _REPOSITORY is None:
        db_path = os.environ.get("UPS_UNIFIED_DB_PATH", "/data/ups2mqtt.db")
        _REPOSITORY = CapabilityRepository(Database(db_path=db_path))
    return _REPOSITORY


def _load_metric_contracts_from_bundle() -> dict[str, dict[str, Any]]:
    path = "/usr/src/app/capabilities/capabilities.json"
    try:
        text = open(path, encoding="utf-8").read()
        parsed = json.loads(text)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    contracts = parsed.get("metric_contracts", {})
    if not isinstance(contracts, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in contracts.items():
        if isinstance(key, str) and isinstance(value, dict):
            out[key] = value
    return out


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_list(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item)]


def _transports_for_profile(
    profile: dict[str, Any], descriptor: Any | None
) -> tuple[str, ...]:
    protocol = str(profile.get("protocol", "")).strip()
    out: list[str] = []

    if protocol == "multi_source":
        active_sources = profile.get("active_sources", {})
        if isinstance(active_sources, dict):
            for key, spec in active_sources.items():
                if isinstance(key, str) and isinstance(spec, dict):
                    out.append(key)
    elif protocol == "hybrid":
        out.extend(["modbus", "snmp"])
    elif protocol:
        out.append(protocol)

    if descriptor and descriptor.supported_sources:
        out.extend([str(item) for item in descriptor.supported_sources])

    deduped: list[str] = []
    seen: set[str] = set()
    for item in out:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return tuple(deduped)
