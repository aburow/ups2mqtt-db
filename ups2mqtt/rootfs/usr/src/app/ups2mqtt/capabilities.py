# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
from pathlib import Path
from typing import Any

from .drivers.runtime_metadata import (
    get_legacy_driver_ids,
    get_migrated_driver_ids,
    load_plugin_capability_profile,
    validate_driver_metadata_ownership,
)

RUNTIME_PROFILE_EXCEPTIONS = (
    OSError,
    ValueError,
    TypeError,
    SyntaxError,
)

EXPECTED_RUNTIME_PROFILE_IDS = frozenset(
    {
        "apc_modbus_smart",
        "apc_modbus_smt",
        "apc_modbus_rack_pdu",
        "cyberpower_modbus_single_phase",
        "cyberpower_modbus_three_phase",
        "ups_snmp_ups_mib",
        "ups_snmp_apc_mib",
    }
)

_DEFAULT_SLOW_INTERVAL = 60
LOG = logging.getLogger("ups2mqtt.capabilities")


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_eval_node(item) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return {
            _eval_node(k): _eval_node(v)
            for k, v in zip(node.keys, node.values, strict=True)
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand)
    raise ValueError(f"Unsupported AST node: {node.__class__.__name__}")


def _read_assignment(path: Path, variable: str) -> Any:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == variable:
                    return _eval_node(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == variable and node.value is not None:
                return _eval_node(node.value)
    raise ValueError(f"{variable} not found in {path}")


def _build_rack_pdu_default_registers() -> list[dict[str, Any]]:
    # Mirrors source defaults for _build_registers(num_phases=1, num_outlets=0, num_banks=0)
    return [
        {
            "key": "num_phases",
            "address": 0x009E,
            "count": 1,
            "type": "uint16",
            "scale": 1,
        },
        {
            "key": "num_metered_phases",
            "address": 0x009F,
            "count": 1,
            "type": "uint16",
            "scale": 1,
        },
        {
            "key": "num_banks",
            "address": 0x00A0,
            "count": 1,
            "type": "uint16",
            "scale": 1,
        },
        {
            "key": "num_outlets",
            "address": 0x00A1,
            "count": 1,
            "type": "uint16",
            "scale": 1,
        },
        {
            "key": "num_metered_outlets",
            "address": 0x00A2,
            "count": 1,
            "type": "uint16",
            "scale": 1,
        },
        {
            "key": "device_real_power",
            "address": 0x00CF,
            "count": 1,
            "type": "int16",
            "scale": 100,
        },
        {
            "key": "device_apparent_power",
            "address": 0x00D0,
            "count": 1,
            "type": "int16",
            "scale": 100,
        },
        {
            "key": "device_power_factor",
            "address": 0x00D1,
            "count": 1,
            "type": "int16",
            "scale": 100,
        },
        {
            "key": "device_energy",
            "address": 0x00D2,
            "count": 2,
            "type": "uint32",
            "scale": 10,
        },
        {
            "key": "device_load_state",
            "address": 0x00D4,
            "count": 1,
            "type": "uint16",
            "scale": 1,
        },
        {
            "key": "phase_L1_current",
            "address": 0x029B,
            "count": 1,
            "type": "int16",
            "scale": 10,
        },
        {
            "key": "phase_L1_voltage",
            "address": 0x029C,
            "count": 1,
            "type": "uint16",
            "scale": 1,
        },
        {
            "key": "phase_L1_real_power",
            "address": 0x029D,
            "count": 1,
            "type": "int16",
            "scale": 1,
        },
        {
            "key": "phase_L1_apparent_power",
            "address": 0x029E,
            "count": 1,
            "type": "int16",
            "scale": 1,
        },
        {
            "key": "phase_L1_power_factor",
            "address": 0x029F,
            "count": 1,
            "type": "int16",
            "scale": 100,
        },
        {
            "key": "phase_L1_state",
            "address": 0x02A0,
            "count": 1,
            "type": "uint16",
            "scale": 1,
        },
    ]


def _load_module(module_path: Path, module_name: str) -> object:
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not create import spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sanitize_poll_groups(raw: Any) -> dict[str, dict[str, int]]:
    groups: dict[str, dict[str, int]] = {}
    if isinstance(raw, dict):
        for group_name, spec in raw.items():
            if not isinstance(group_name, str) or not group_name.strip():
                continue
            if not isinstance(spec, dict):
                continue
            interval = spec.get("interval_s")
            try:
                interval_int = int(interval)
            except (TypeError, ValueError):
                continue
            if interval_int <= 0:
                continue
            groups[group_name] = {"interval_s": interval_int}
    if "slow" not in groups:
        groups["slow"] = {"interval_s": _DEFAULT_SLOW_INTERVAL}
    return groups


def _sanitize_key_precedence(raw: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            continue
        if value in {"modbus", "snmp"}:
            out[key] = str(value)
    return out


def _sanitize_registers(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("key")]


def _sanitize_register_blocks(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _sanitize_oids(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, spec in raw.items():
        if not isinstance(key, str) or not isinstance(spec, dict):
            continue
        if "oid" not in spec and "oids" not in spec:
            continue
        out[key] = spec
    return out


def _sanitize_snmp_blocks(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        metrics = item.get("metrics")
        if metrics is not None and not isinstance(metrics, list):
            continue
        out.append(item)
    return out


def _sanitize_nut_variables(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for var_name, spec in raw.items():
        if not isinstance(var_name, str) or not isinstance(spec, dict):
            continue
        key = spec.get("key")
        if not isinstance(key, str) or not key:
            continue
        out[var_name] = spec
    return out


def _sanitize_nut_status_map(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for status_token, spec in raw.items():
        if not isinstance(status_token, str) or not isinstance(spec, dict):
            continue
        key = spec.get("key")
        if not isinstance(key, str) or not key:
            continue
        out[status_token] = spec
    return out


def _collect_metric_keys(profile: dict[str, Any]) -> set[str]:
    protocol = profile.get("protocol")
    if protocol == "modbus":
        return {str(item["key"]) for item in profile.get("registers", [])}
    if protocol == "snmp":
        return set(profile.get("oids", {}).keys())
    if protocol == "hybrid":
        mod_keys = {
            str(item["key"])
            for item in profile.get("modbus", {}).get("registers", [])
            if isinstance(item, dict) and "key" in item
        }
        snmp_keys = set(profile.get("snmp", {}).get("oids", {}).keys())
        return mod_keys | snmp_keys
    if protocol == "nut":
        nut = profile.get("nut", {})
        if not isinstance(nut, dict):
            return set()
        var_keys = {
            str(spec.get("key"))
            for spec in nut.get("variables", {}).values()
            if isinstance(spec, dict) and isinstance(spec.get("key"), str)
        }
        status_keys = {
            str(spec.get("key"))
            for spec in nut.get("status_map", {}).values()
            if isinstance(spec, dict) and isinstance(spec.get("key"), str)
        }
        return var_keys | status_keys
    return set()


def _validate_profile(profile: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    profile_id = str(profile.get("profile_id", "<unknown>"))
    protocol = profile.get("protocol")
    poll_groups = profile.get("poll_groups", {})
    if not isinstance(poll_groups, dict) or "slow" not in poll_groups:
        errors.append(f"{profile_id}: poll_groups must include slow")
        poll_groups = {"slow": {"interval_s": _DEFAULT_SLOW_INTERVAL}}

    if protocol == "modbus":
        registers = profile.get("registers", [])
        keys = [str(item["key"]) for item in registers if isinstance(item, dict)]
        if len(keys) != len(set(keys)):
            errors.append(f"{profile_id}: duplicate modbus register keys")
        for item in registers:
            if not isinstance(item, dict):
                continue
            group = str(item.get("poll_group", "slow"))
            if group not in poll_groups:
                errors.append(
                    f"{profile_id}.{item.get('key')}: unknown poll_group {group}"
                )
        for block in profile.get("register_blocks", []):
            if not isinstance(block, dict):
                continue
            group = str(block.get("poll_group", "slow"))
            if group not in poll_groups:
                errors.append(
                    f"{profile_id}.{block.get('name', 'register_block')}: unknown poll_group {group}"
                )
        return errors

    if protocol == "snmp":
        oids = profile.get("oids", {})
        if not isinstance(oids, dict):
            errors.append(f"{profile_id}: oids must be a mapping")
            return errors
        keys = list(oids.keys())
        if len(keys) != len(set(keys)):
            errors.append(f"{profile_id}: duplicate snmp keys")
        for key, spec in oids.items():
            if not isinstance(spec, dict):
                continue
            group = str(spec.get("poll_group", "slow"))
            if group not in poll_groups:
                errors.append(f"{profile_id}.{key}: unknown poll_group {group}")
        for block in profile.get("snmp_blocks", []):
            if not isinstance(block, dict):
                continue
            group = str(block.get("poll_group", "slow"))
            if group not in poll_groups:
                errors.append(
                    f"{profile_id}.{block.get('name', 'snmp_block')}: unknown poll_group {group}"
                )
            metrics = block.get("metrics", [])
            if isinstance(metrics, list):
                for metric in metrics:
                    if str(metric) not in oids:
                        errors.append(
                            f"{profile_id}.{block.get('name', 'snmp_block')}: unknown metric {metric}"
                        )
        return errors

    if protocol == "nut":
        nut = profile.get("nut", {})
        if not isinstance(nut, dict):
            errors.append(f"{profile_id}: nut profile requires nut mapping")
            return errors
        variables = nut.get("variables", {})
        if not isinstance(variables, dict):
            errors.append(f"{profile_id}: nut.variables must be a mapping")
            return errors
        if not variables:
            errors.append(f"{profile_id}: nut.variables must not be empty")
            return errors
        for var_name, spec in variables.items():
            if not isinstance(spec, dict):
                continue
            key = spec.get("key")
            if not isinstance(key, str) or not key:
                errors.append(f"{profile_id}.{var_name}: nut variable key missing")
                continue
            group = str(spec.get("poll_group", "slow"))
            if group not in poll_groups:
                errors.append(f"{profile_id}.{key}: unknown poll_group {group}")
        status_map = nut.get("status_map", {})
        if status_map is not None and not isinstance(status_map, dict):
            errors.append(f"{profile_id}: nut.status_map must be a mapping")
        return errors

    if protocol != "hybrid":
        errors.append(f"{profile_id}: unsupported protocol {protocol}")
        return errors

    modbus = profile.get("modbus", {})
    snmp = profile.get("snmp", {})
    if not isinstance(modbus, dict) or not isinstance(snmp, dict):
        errors.append(f"{profile_id}: hybrid requires modbus and snmp mappings")
        return errors

    mod_keys = {
        str(item["key"])
        for item in modbus.get("registers", [])
        if isinstance(item, dict) and "key" in item
    }
    snmp_oids = snmp.get("oids", {})
    snmp_keys = set(snmp_oids.keys()) if isinstance(snmp_oids, dict) else set()
    collisions = mod_keys & snmp_keys
    precedence = profile.get("key_precedence", {})
    if not isinstance(precedence, dict):
        precedence = {}
    missing = sorted(key for key in collisions if key not in precedence)
    if missing:
        errors.append(
            f"{profile_id}: key_precedence missing for collisions: {', '.join(missing)}"
        )

    for item in modbus.get("registers", []):
        if not isinstance(item, dict):
            continue
        group = str(item.get("poll_group", "slow"))
        if group not in poll_groups:
            errors.append(f"{profile_id}.{item.get('key')}: unknown poll_group {group}")
    for block in modbus.get("register_blocks", []):
        if not isinstance(block, dict):
            continue
        group = str(block.get("poll_group", "slow"))
        if group not in poll_groups:
            errors.append(
                f"{profile_id}.{block.get('name', 'register_block')}: unknown poll_group {group}"
            )
    if isinstance(snmp_oids, dict):
        for key, spec in snmp_oids.items():
            if not isinstance(spec, dict):
                continue
            group = str(spec.get("poll_group", "slow"))
            if group not in poll_groups:
                errors.append(f"{profile_id}.{key}: unknown poll_group {group}")
    for block in snmp.get("snmp_blocks", []):
        if not isinstance(block, dict):
            continue
        group = str(block.get("poll_group", "slow"))
        if group not in poll_groups:
            errors.append(
                f"{profile_id}.{block.get('name', 'snmp_block')}: unknown poll_group {group}"
            )
        metrics = block.get("metrics", [])
        if isinstance(metrics, list) and isinstance(snmp_oids, dict):
            for metric in metrics:
                if str(metric) not in snmp_oids:
                    errors.append(
                        f"{profile_id}.{block.get('name', 'snmp_block')}: unknown metric {metric}"
                    )
    return errors


def _normalize_v2_profile(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    profile_id = str(raw.get("profile_id", "")).strip()
    protocol = str(raw.get("protocol", "")).strip()
    normalized: dict[str, Any] = {
        "profile_id": profile_id,
        "protocol": protocol,
        "poll_groups": _sanitize_poll_groups(raw.get("poll_groups")),
    }
    if protocol == "modbus":
        normalized["registers"] = _sanitize_registers(raw.get("registers"))
        normalized["register_blocks"] = _sanitize_register_blocks(
            raw.get("register_blocks")
        )
    elif protocol == "snmp":
        normalized["oids"] = _sanitize_oids(raw.get("oids"))
        normalized["snmp_blocks"] = _sanitize_snmp_blocks(raw.get("snmp_blocks"))
    elif protocol == "hybrid":
        modbus = raw.get("modbus", {})
        snmp = raw.get("snmp", {})
        normalized["modbus"] = {
            "registers": _sanitize_registers(
                modbus.get("registers") if isinstance(modbus, dict) else None
            ),
            "register_blocks": _sanitize_register_blocks(
                modbus.get("register_blocks") if isinstance(modbus, dict) else None
            ),
        }
        normalized["snmp"] = {
            "oids": _sanitize_oids(
                snmp.get("oids") if isinstance(snmp, dict) else None
            ),
            "snmp_blocks": _sanitize_snmp_blocks(
                snmp.get("snmp_blocks") if isinstance(snmp, dict) else None
            ),
        }
        normalized["key_precedence"] = _sanitize_key_precedence(
            raw.get("key_precedence")
        )
    elif protocol == "nut":
        nut = raw.get("nut", {})
        normalized["nut"] = {
            "variables": _sanitize_nut_variables(
                nut.get("variables") if isinstance(nut, dict) else None
            ),
            "status_map": _sanitize_nut_status_map(
                nut.get("status_map") if isinstance(nut, dict) else None
            ),
        }
    else:
        normalized["protocol"] = protocol
    errors = _validate_profile(normalized)
    return normalized, errors


def _extract_profiles_from_contract_module(module: object) -> list[dict[str, Any]]:
    if hasattr(module, "PROFILES"):
        data = getattr(module, "PROFILES")
        if isinstance(data, (list, tuple)):
            return [item for item in data if isinstance(item, dict)]
    if hasattr(module, "CAPABILITY_SOURCE"):
        source = getattr(module, "CAPABILITY_SOURCE")
        if isinstance(source, dict):
            profiles = source.get("profiles")
            if isinstance(profiles, dict):
                return [item for item in profiles.values() if isinstance(item, dict)]
            if isinstance(profiles, (list, tuple)):
                return [item for item in profiles if isinstance(item, dict)]
    if hasattr(module, "CAPABILITY_PROFILES"):
        profiles = getattr(module, "CAPABILITY_PROFILES")
        if isinstance(profiles, dict):
            return [item for item in profiles.values() if isinstance(item, dict)]
        if isinstance(profiles, (list, tuple)):
            return [item for item in profiles if isinstance(item, dict)]
    return []


# Mapping from profile_id to the legacy REGISTERS file path (relative to apps_dir).
# Used to supplement v2 contract profiles with raw register descriptors that the
# contract intentionally omits (e.g. bitfield source registers for derived sensors).
_LEGACY_REGISTERS_FILES: dict[str, tuple[str, str]] = {
    "apc_modbus_smart": (
        "apc-modbus-ha",
        "custom_components/apc_modbus/registers_smart_ups.py",
    ),
    "apc_modbus_smt": (
        "apc-modbus-ha",
        "custom_components/apc_modbus/registers_smt_ups.py",
    ),
    "apc_modbus_rack_pdu": (
        "apc-modbus-ha",
        "custom_components/apc_modbus/registers_rack_pdu.py",
    ),
    "cyberpower_modbus_single_phase": (
        "cyberpower-modbus-ha",
        "custom_components/cyberpower_modbus/registers_single_phase.py",
    ),
    "cyberpower_modbus_three_phase": (
        "cyberpower-modbus-ha",
        "custom_components/cyberpower_modbus/registers_three_phase.py",
    ),
}


def _merge_legacy_registers_into_v2_profile(
    profile: dict[str, Any], apps_dir: Path, profile_id: str
) -> None:
    """Supplement a normalized v2 contract profile's registers list with raw register
    descriptors from the legacy register file.

    The v2 contract intentionally exposes only a curated subset of metrics.
    Catalog-backed sensors (especially derived bit-field sensors) depend on raw source
    registers (e.g. ups_status_bf, battery_system_error_bf) that the contract omits.
    By merging the legacy REGISTERS into the profile, block reads will extract these
    raw values so that post-poll bit derivation can produce the catalog-derived keys.

    Only registers whose keys are NOT already present in the contract are added.
    The contract's existing register descriptors (addresses, scales, poll groups) are
    always preserved unchanged.
    """
    spec = _LEGACY_REGISTERS_FILES.get(profile_id)
    if spec is None:
        return

    app_dir_name, registers_rel = spec
    legacy_path = apps_dir / app_dir_name / registers_rel
    if not legacy_path.exists():
        return

    try:
        legacy_registers = _read_assignment(legacy_path, "REGISTERS")
    except RUNTIME_PROFILE_EXCEPTIONS:
        return

    if not isinstance(legacy_registers, list):
        return

    protocol = profile.get("protocol")
    if protocol == "modbus":
        existing = profile.get("registers", [])
        existing_keys = {
            str(r["key"]) for r in existing if isinstance(r, dict) and "key" in r
        }
        for reg in legacy_registers:
            if not isinstance(reg, dict) or "key" not in reg or "address" not in reg:
                continue
            if str(reg["key"]) in existing_keys:
                continue
            # Preserve poll_group from legacy register if specified, otherwise use "slow" default.
            # Derived sensor logic will cache slow-polled raw values for reuse across fast cycles.
            merged_reg = dict(reg)
            merged_reg.setdefault("poll_group", "slow")
            existing.append(merged_reg)
            existing_keys.add(str(reg["key"]))
    elif protocol == "hybrid":
        modbus_section = profile.get("modbus", {})
        if not isinstance(modbus_section, dict):
            return
        existing = modbus_section.get("registers", [])
        existing_keys = {
            str(r["key"]) for r in existing if isinstance(r, dict) and "key" in r
        }
        for reg in legacy_registers:
            if not isinstance(reg, dict) or "key" not in reg or "address" not in reg:
                continue
            if str(reg["key"]) in existing_keys:
                continue
            # Preserve poll_group from legacy register if specified, otherwise use "slow" default.
            # Derived sensor logic will cache slow-polled raw values for reuse across fast cycles.
            merged_reg = dict(reg)
            merged_reg.setdefault("poll_group", "slow")
            existing.append(merged_reg)
            existing_keys.add(str(reg["key"]))


def _load_v2_profiles(
    apps_dir: Path,
    *,
    allowed_profile_ids: set[str],
    migrated_profile_ids: set[str],
) -> tuple[dict[str, Any], list[str]]:
    contract_files = [
        (
            "apc-modbus-ha",
            "custom_components/apc_modbus/capability_profiles_unified.py",
            {"apc_modbus_smart", "apc_modbus_smt", "apc_modbus_rack_pdu"},
        ),
        (
            "cyberpower-modbus-ha",
            "custom_components/cyberpower_modbus/capability_profile_unified.py",
            {
                "cyberpower_modbus_single_phase",
                "cyberpower_modbus_three_phase",
            },
        ),
        (
            "ups-snmp-ha",
            "custom_components/ups_snmp_ha/capability_profile_unified.py",
            {"ups_snmp_ups_mib", "ups_snmp_apc_mib"},
        ),
    ]
    profiles: dict[str, Any] = {}
    errors: list[str] = []
    for app_name, module_rel, app_profile_ids in contract_files:
        if not (allowed_profile_ids & app_profile_ids):
            continue
        module_path = apps_dir / app_name / module_rel
        if not module_path.exists():
            continue
        try:
            module = _load_module(
                module_path, f"ups_unified_{app_name.replace('-', '_')}_contract_v2"
            )
            raw_profiles = _extract_profiles_from_contract_module(module)
            if not raw_profiles:
                errors.append(f"{app_name}: no profiles found in {module_rel}")
                continue
            for raw in raw_profiles:
                normalized, profile_errors = _normalize_v2_profile(raw)
                profile_id = str(normalized.get("profile_id", "")).strip()
                if not profile_id:
                    errors.append(f"{app_name}: profile missing profile_id")
                    continue
                if profile_id not in allowed_profile_ids:
                    # Contract modules may contain both migrated and legacy profile blocks.
                    # Only allowed_profile_ids are eligible for legacy loading in this pass.
                    continue
                if profile_errors:
                    errors.extend(profile_errors)
                    continue
                # Supplement the normalized profile with raw register descriptors from
                # the legacy register file so that block reads extract the source
                # registers needed for catalog-backed derived (bit-flag) sensors.
                _merge_legacy_registers_into_v2_profile(
                    normalized, apps_dir, profile_id
                )
                profiles[profile_id] = normalized
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            errors.append(f"{app_name}: contract load failed: {err}")
    return profiles, errors


def _runtime_profiles_legacy(
    apps_dir: Path,
    *,
    allowed_profile_ids: set[str],
    migrated_profile_ids: set[str],
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    apc_root = apps_dir / "apc-modbus-ha" / "custom_components" / "apc_modbus"
    snmp_root = apps_dir / "ups-snmp-ha" / "custom_components" / "ups_snmp_ha"
    cyber_root = (
        apps_dir / "cyberpower-modbus-ha" / "custom_components" / "cyberpower_modbus"
    )

    def _modbus_profile(path: Path) -> dict[str, Any]:
        profile: dict[str, Any] = {
            "protocol": "modbus",
            "registers": _read_assignment(path, "REGISTERS"),
        }
        try:
            profile["register_blocks"] = _read_assignment(path, "REGISTER_BLOCKS")
        except RUNTIME_PROFILE_EXCEPTIONS:
            profile["register_blocks"] = []
        return profile

    profiles: dict[str, Any] = {}

    def _reject_if_migrated(profile_id: str, source_label: str) -> bool:
        if profile_id in migrated_profile_ids:
            message = (
                "Mixed metadata sourcing detected: "
                f"migrated driver {profile_id} attempted legacy {source_label} load"
            )
            LOG.error(message)
            raise RuntimeError(message)
        return False

    if "apc_modbus_smart" in allowed_profile_ids and not _reject_if_migrated(
        "apc_modbus_smart", "registers"
    ):
        path = apc_root / "registers_smart_ups.py"
        if not path.exists():
            return {}, errors
        profiles["apc_modbus_smart"] = _modbus_profile(path)

    if "apc_modbus_smt" in allowed_profile_ids and not _reject_if_migrated(
        "apc_modbus_smt", "registers"
    ):
        path = apc_root / "registers_smt_ups.py"
        if not path.exists():
            return {}, errors
        profiles["apc_modbus_smt"] = _modbus_profile(path)

    if "apc_modbus_rack_pdu" in allowed_profile_ids and not _reject_if_migrated(
        "apc_modbus_rack_pdu", "registers"
    ):
        path = apc_root / "registers_rack_pdu.py"
        if not path.exists():
            return {}, errors
        try:
            profiles["apc_modbus_rack_pdu"] = _modbus_profile(path)
        except RUNTIME_PROFILE_EXCEPTIONS:
            blocks = []
            try:
                blocks = _read_assignment(path, "REGISTER_BLOCKS")
            except RUNTIME_PROFILE_EXCEPTIONS:
                blocks = []
            profiles["apc_modbus_rack_pdu"] = {
                "protocol": "modbus",
                "registers": _build_rack_pdu_default_registers(),
                "register_blocks": blocks,
            }

    if (
        "cyberpower_modbus_single_phase" in allowed_profile_ids
        and not _reject_if_migrated("cyberpower_modbus_single_phase", "registers")
    ):
        path = cyber_root / "registers_single_phase.py"
        if not path.exists():
            return {}, errors
        profiles["cyberpower_modbus_single_phase"] = _modbus_profile(path)

    if (
        "cyberpower_modbus_three_phase" in allowed_profile_ids
        and not _reject_if_migrated("cyberpower_modbus_three_phase", "registers")
    ):
        path = cyber_root / "registers_three_phase.py"
        if not path.exists():
            return {}, errors
        profiles["cyberpower_modbus_three_phase"] = _modbus_profile(path)

    if (
        "ups_snmp_ups_mib" in allowed_profile_ids
        or "ups_snmp_apc_mib" in allowed_profile_ids
    ):
        coordinator = snmp_root / "coordinator.py"
        if not coordinator.exists():
            return {}, errors
        if "ups_snmp_ups_mib" in allowed_profile_ids and not _reject_if_migrated(
            "ups_snmp_ups_mib", "coordinator"
        ):
            profiles["ups_snmp_ups_mib"] = {
                "protocol": "snmp",
                "oids": _read_assignment(coordinator, "UPS_MIB_OIDS"),
            }
        if "ups_snmp_apc_mib" in allowed_profile_ids and not _reject_if_migrated(
            "ups_snmp_apc_mib", "coordinator"
        ):
            profiles["ups_snmp_apc_mib"] = {
                "protocol": "snmp",
                "oids": _read_assignment(coordinator, "APC_MIB_OIDS"),
            }
    return profiles, errors


def _runtime_profiles(
    apps_dir: Path,
    *,
    allowed_profile_ids: set[str],
    migrated_profile_ids: set[str],
) -> tuple[dict[str, Any], str, list[str]]:
    errors: list[str] = []
    profiles, v2_errors = _load_v2_profiles(
        apps_dir,
        allowed_profile_ids=allowed_profile_ids,
        migrated_profile_ids=migrated_profile_ids,
    )
    errors.extend(v2_errors)
    if profiles:
        missing = allowed_profile_ids - set(profiles.keys())
        if not missing:
            return profiles, "runtime_apps_contract_v2", errors
        errors.append(
            "contract_v2 missing expected profiles: " + ", ".join(sorted(missing))
        )

    legacy_profiles, legacy_errors = _runtime_profiles_legacy(
        apps_dir,
        allowed_profile_ids=allowed_profile_ids,
        migrated_profile_ids=migrated_profile_ids,
    )
    errors.extend(legacy_errors)
    if legacy_profiles:
        return legacy_profiles, "runtime_apps_legacy", errors
    return {}, "none", errors


def _discover_runtime_profiles(
    *,
    allowed_profile_ids: set[str],
    migrated_profile_ids: set[str],
) -> tuple[dict[str, Any], str, list[str]]:
    if not allowed_profile_ids:
        return {}, "none", []
    explicit = os.environ.get("UPS_UNIFIED_APPS_DIR")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend([Path("/apps"), Path("/data/apps")])

    all_errors: list[str] = []
    for candidate in candidates:
        try:
            profiles, source, errors = _runtime_profiles(
                candidate,
                allowed_profile_ids=allowed_profile_ids,
                migrated_profile_ids=migrated_profile_ids,
            )
            all_errors.extend(errors)
        except RUNTIME_PROFILE_EXCEPTIONS:
            profiles = {}
            source = "none"
        if profiles:
            return profiles, source, all_errors
    return {}, "none", all_errors


def load_capabilities(
    path: str = "/usr/src/app/capabilities/capabilities.json",
) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    bundled_profiles = data.get("profiles", {})
    if not isinstance(bundled_profiles, dict) or not bundled_profiles:
        raise ValueError(f"No profiles found in {path}")

    validate_driver_metadata_ownership()
    migrated_profile_ids = get_migrated_driver_ids()
    legacy_profile_ids = get_legacy_driver_ids() & EXPECTED_RUNTIME_PROFILE_IDS

    plugin_profiles: dict[str, Any] = {}
    plugin_errors: list[str] = []
    for driver_id in sorted(migrated_profile_ids):
        try:
            profile = load_plugin_capability_profile(driver_id)
            plugin_profiles[driver_id] = profile
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            plugin_errors.append(f"{driver_id}: plugin profile load failed: {err}")

    runtime_profiles, runtime_source, runtime_errors = _discover_runtime_profiles(
        allowed_profile_ids=legacy_profile_ids,
        migrated_profile_ids=migrated_profile_ids,
    )
    mixed_drivers = migrated_profile_ids & set(runtime_profiles.keys())
    if mixed_drivers:
        message = "Mixed metadata sourcing detected for migrated drivers: " + ", ".join(
            sorted(mixed_drivers)
        )
        LOG.error(message)
        raise RuntimeError(message)

    runtime_errors.extend(plugin_errors)
    if plugin_profiles:
        runtime_profiles = dict(runtime_profiles)
        runtime_profiles.update(plugin_profiles)
        runtime_source = (
            f"{runtime_source}+plugin"
            if runtime_source != "none"
            else "runtime_plugin_registry"
        )
    if runtime_profiles:
        merged_profiles = dict(bundled_profiles)
        merged_profiles.update(runtime_profiles)
        payload: dict[str, Any] = {
            "source": runtime_source,
            "profiles": merged_profiles,
        }
        for key in ("metadata", "metric_contracts"):
            if key in data:
                payload[key] = data[key]
        if runtime_errors:
            payload["validation_errors"] = runtime_errors
        return payload

    if runtime_errors:
        data["validation_errors"] = runtime_errors
    return data


def source_keys(profile: dict[str, Any]) -> list[str]:
    protocol = profile.get("protocol")
    if protocol == "modbus":
        return [
            str(item["key"])
            for item in profile.get("registers", [])
            if isinstance(item, dict) and "key" in item
        ]
    if protocol == "snmp":
        return [str(key) for key in profile.get("oids", {}).keys()]
    if protocol == "hybrid":
        modbus_keys = [
            str(item["key"])
            for item in profile.get("modbus", {}).get("registers", [])
            if isinstance(item, dict) and "key" in item
        ]
        snmp_keys = [
            str(key)
            for key in profile.get("snmp", {}).get("oids", {}).keys()
            if key not in set(modbus_keys)
        ]
        return modbus_keys + snmp_keys
    if protocol == "multi_source":
        # Multi-source drivers use active_sources structure
        # Extract keys from all transports (modbus + snmp)
        keys: list[str] = []
        active_sources = profile.get("active_sources", {})

        # Get modbus register keys
        modbus_config = active_sources.get("modbus", {})
        if isinstance(modbus_config, dict):
            registers = modbus_config.get("registers", [])
            for item in registers:
                if isinstance(item, dict) and "key" in item:
                    keys.append(str(item["key"]))

        # Get snmp oid keys (exclude duplicates)
        snmp_config = active_sources.get("snmp", {})
        if isinstance(snmp_config, dict):
            oids = snmp_config.get("oids", {})
            if isinstance(oids, dict):
                existing = set(keys)
                for key in oids.keys():
                    if key not in existing:
                        keys.append(str(key))

        return keys
    if protocol == "nut":
        keys: list[str] = []
        nut = profile.get("nut", {})
        if not isinstance(nut, dict):
            return keys
        variables = nut.get("variables", {})
        if isinstance(variables, dict):
            for spec in variables.values():
                if isinstance(spec, dict) and isinstance(spec.get("key"), str):
                    keys.append(str(spec["key"]))
        status_map = nut.get("status_map", {})
        if isinstance(status_map, dict):
            for spec in status_map.values():
                if isinstance(spec, dict) and isinstance(spec.get("key"), str):
                    key = str(spec["key"])
                    if key not in set(keys):
                        keys.append(key)
        return keys
    return []


def bundled_source_keys(
    source: str, path: str = "/usr/src/app/capabilities/capabilities.json"
) -> list[str]:
    """Return metric keys for a source from bundled capabilities only."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        return []
    profile = profiles.get(source)
    if not isinstance(profile, dict):
        return []
    return source_keys(profile)


def poll_group_intervals(
    profile: dict[str, Any], default_interval: int
) -> dict[str, int]:
    """Resolve poll group intervals for a profile."""
    fallback = max(1, int(default_interval))
    raw = profile.get("poll_groups", {})
    groups = _sanitize_poll_groups(raw)
    out: dict[str, int] = {}
    for name, spec in groups.items():
        interval = int(spec.get("interval_s", fallback))
        out[name] = max(1, interval)
    if "slow" not in out:
        out["slow"] = fallback
    return out
