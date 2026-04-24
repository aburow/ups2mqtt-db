# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from time import monotonic
from typing import Any

from .catalog import get_catalog_derived_metrics

LOG = logging.getLogger("ups2mqtt")

_MISSING_SOURCE_LOG_INTERVAL_S = 60.0
_MISSING_SOURCE_WARNINGS: dict[tuple[str, str, str], float] = {}
_SUPPORTED_TRANSFORMS = {
    "bitfield_bit_to_bool",
    "enum_map",
    "days_since_epoch_to_date",
}
_SUPPORTED_OUTPUT_TYPES = {"bool", "number", "string", "date", "datetime"}


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"Cannot parse bool from {value!r}")


def _type_matches(output_type: str, value: Any) -> bool:
    if output_type == "bool":
        return isinstance(value, bool)
    if output_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if output_type == "string":
        return isinstance(value, str)
    if output_type == "date":
        if not isinstance(value, str):
            return False
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return True
        except ValueError:
            return False
    if output_type == "datetime":
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    return False


def _resolve_source_value(
    *,
    values: dict[str, Any],
    value_cache: dict[str, Any],
    source_key: str,
) -> tuple[Any, str] | None:
    current_value = values.get(source_key)
    if current_value is not None:
        return current_value, "current"
    cached_value = value_cache.get(source_key)
    if cached_value is not None:
        return cached_value, "cache"
    return None


def _rate_limited_missing_source_warning(
    *,
    device_uid: str,
    output_key: str,
) -> None:
    rate_key = (device_uid, output_key, "missing_source")
    now = monotonic()
    last_logged = _MISSING_SOURCE_WARNINGS.get(rate_key, 0.0)
    if now - last_logged < _MISSING_SOURCE_LOG_INTERVAL_S:
        return
    _MISSING_SOURCE_WARNINGS[rate_key] = now
    LOG.warning("Transform skipped (missing source): %s", output_key)


def _transform_bitfield_bit_to_bool(source_value: Any, params: dict[str, Any]) -> bool:
    bit = params.get("bit")
    if bit is None:
        raise ValueError("bitfield_bit_to_bool requires params.bit")
    bit_index = int(bit)
    raw_int = int(source_value)
    return bool(raw_int & (1 << bit_index))


def _transform_enum_map(source_value: Any, params: dict[str, Any]) -> str | None:
    mapping = params.get("map")
    if not isinstance(mapping, dict):
        raise ValueError("enum_map requires params.map dict")
    # Accept both int and str map keys.
    candidates = [source_value]
    try:
        candidates.append(int(source_value))
    except (TypeError, ValueError):
        pass
    candidates.extend([str(source_value)])
    for candidate in candidates:
        if candidate in mapping:
            mapped = mapping[candidate]
            if mapped is None:
                return None
            return str(mapped)
    raise ValueError(f"enum_map has no mapping for {source_value!r}")


def _transform_days_since_epoch_to_date(
    source_value: Any, params: dict[str, Any]
) -> str:
    epoch_raw = params.get("epoch")
    if not isinstance(epoch_raw, str) or not epoch_raw:
        raise ValueError("days_since_epoch_to_date requires params.epoch YYYY-MM-DD")
    epoch = datetime.strptime(epoch_raw, "%Y-%m-%d").date()
    days = int(source_value)
    return (epoch + timedelta(days=days)).isoformat()


def _run_transform(
    *,
    transform_name: str,
    source_value: Any,
    params: dict[str, Any],
) -> Any:
    if transform_name == "bitfield_bit_to_bool":
        return _transform_bitfield_bit_to_bool(source_value, params)
    if transform_name == "enum_map":
        return _transform_enum_map(source_value, params)
    if transform_name == "days_since_epoch_to_date":
        return _transform_days_since_epoch_to_date(source_value, params)
    raise ValueError(f"Unsupported transform type: {transform_name}")


def _validate_transform_declaration(
    declaration: dict[str, Any],
) -> tuple[str, str, str, str, bool, dict[str, Any]] | None:
    output_key = str(declaration.get("output_key", "")).strip()
    source_key = str(declaration.get("source_key", "")).strip()
    transform_name = str(declaration.get("transform", "")).strip()
    output_type = str(declaration.get("output_type", "")).strip().lower()
    null_to_value_fill = bool(declaration.get("null_to_value_fill", True))
    params_raw = declaration.get("params", {})
    params = params_raw if isinstance(params_raw, dict) else {}

    context = output_key or "<missing_output_key>"

    if not output_key:
        LOG.error("Transform declaration rejected: %s (missing output_key)", context)
        return None
    if not source_key:
        LOG.error("Transform declaration rejected: %s (missing source_key)", context)
        return None
    if not transform_name:
        LOG.error("Transform declaration rejected: %s (missing transform)", context)
        return None
    if transform_name not in _SUPPORTED_TRANSFORMS:
        LOG.error(
            "Transform declaration rejected: %s (unsupported transform: %s)",
            context,
            transform_name,
        )
        return None
    if output_type not in _SUPPORTED_OUTPUT_TYPES:
        LOG.error(
            "Transform declaration rejected: %s (invalid output_type: %s)",
            context,
            output_type or "<missing>",
        )
        return None

    if transform_name == "bitfield_bit_to_bool":
        bit = params.get("bit")
        try:
            bit_index = int(bit)
        except (TypeError, ValueError):
            LOG.error(
                "Transform declaration rejected: %s (bitfield_bit_to_bool requires params.bit integer)",
                context,
            )
            return None
        if bit_index < 0:
            LOG.error(
                "Transform declaration rejected: %s (bitfield_bit_to_bool requires non-negative params.bit)",
                context,
            )
            return None
    elif transform_name == "enum_map":
        mapping = params.get("map")
        if not isinstance(mapping, dict) or not mapping:
            LOG.error(
                "Transform declaration rejected: %s (enum_map requires non-empty params.map mapping)",
                context,
            )
            return None
    elif transform_name == "days_since_epoch_to_date":
        epoch_raw = params.get("epoch")
        if not isinstance(epoch_raw, str) or not epoch_raw:
            LOG.error(
                "Transform declaration rejected: %s (days_since_epoch_to_date requires params.epoch YYYY-MM-DD)",
                context,
            )
            return None
        try:
            datetime.strptime(epoch_raw, "%Y-%m-%d")
        except ValueError:
            LOG.error(
                "Transform declaration rejected: %s (invalid params.epoch format, expected YYYY-MM-DD)",
                context,
            )
            return None

    return (
        output_key,
        source_key,
        transform_name,
        output_type,
        null_to_value_fill,
        params,
    )


def apply_catalog_transforms(
    values: dict[str, Any],
    *,
    device_uid: str,
    runtime_source: str,
    apps_dir: str | None,
    value_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply catalog-declared transforms to polled values (v1.1 contract)."""
    output = dict(values)
    if value_cache is None:
        value_cache = {}
    for key, value in output.items():
        if value is not None:
            value_cache[key] = value

    if not apps_dir or not runtime_source:
        return output

    try:
        transforms = get_catalog_derived_metrics(runtime_source, apps_dir)
    except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
        LOG.error("Transform failed: catalog_load (%s)", err)
        return output

    # No chaining in v1. Any transform sourcing another declared output key is skipped.
    declared_output_keys: set[str] = set()
    for declaration in transforms:
        output_key = str(declaration.get("output_key", "")).strip()
        if output_key:
            declared_output_keys.add(output_key)

    for declaration in transforms:
        validated = _validate_transform_declaration(declaration)
        if validated is None:
            continue
        (
            output_key,
            source_key,
            transform_name,
            output_type,
            null_to_value_fill,
            params,
        ) = validated
        if source_key in declared_output_keys:
            continue

        existing_value = output.get(output_key)
        if existing_value is not None:
            continue
        if output_key in output and existing_value is None and not null_to_value_fill:
            continue

        resolved = _resolve_source_value(
            values=output,
            value_cache=value_cache,
            source_key=source_key,
        )
        if resolved is None:
            _rate_limited_missing_source_warning(
                device_uid=device_uid,
                output_key=output_key,
            )
            continue
        source_value, source_origin = resolved

        try:
            transformed_value = _run_transform(
                transform_name=transform_name,
                source_value=source_value,
                params=params,
            )
            if transformed_value is None:
                continue
            if not _type_matches(output_type, transformed_value):
                raise ValueError(
                    f"type mismatch: expected {output_type}, got {type(transformed_value).__name__}"
                )
            output[output_key] = transformed_value
            value_cache[output_key] = transformed_value
            LOG.debug(
                "Transform applied: %s from %s (source=%s)",
                output_key,
                source_key,
                source_origin,
            )
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            LOG.error("Transform failed: %s (%s)", output_key, err)
            continue

    return output
