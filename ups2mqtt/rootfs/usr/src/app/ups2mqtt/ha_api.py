# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""Home Assistant API client for entity registry operations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    import aiohttp
except ImportError:
    aiohttp = None

LOG = logging.getLogger("ups2mqtt.ha_api")


async def _ws_recv_result(
    ws: aiohttp.ClientWebSocketResponse, request_id: int
) -> dict[str, Any]:
    while True:
        message = await ws.receive_json()
        if (
            isinstance(message, dict)
            and message.get("type") == "result"
            and message.get("id") == request_id
        ):
            return message


def _build_ha_ws_url(ha_url: str) -> str:
    base = ha_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme == "https":
        ws_scheme = "wss"
    else:
        ws_scheme = "ws"
    ws_base = urlunsplit((ws_scheme, parsed.netloc, parsed.path, "", ""))
    return f"{ws_base}/api/websocket"


async def delete_device_entities(
    ha_url: str, ha_token: str, device_identity: str
) -> dict[str, Any]:
    """Delete all Home Assistant entities for a device from entity registry.

    Uses Home Assistant's websocket entity_registry APIs to remove entities by
    unique_id prefix. This is required for MQTT discovery reinitialize flows
    where entity enabled/disabled defaults must be recalculated.

    Args:
        ha_url: Base URL of Home Assistant (e.g., "http://homeassistant.local:8123")
        ha_token: Long-lived access token for Home Assistant API
        device_identity: Device UUID/identity used in discovery unique_id
            (e.g. "02faf945-..." for unique_id "ups_unified_<id>_<metric>")

    Returns:
        Dict with keys:
        - "deleted": list of deleted entity IDs
        - "error": error message if operation failed
        - "skipped": True if HA is not configured
    """
    if not ha_url or not ha_token:
        return {"skipped": True, "reason": "HA URL or token not configured"}

    if aiohttp is None:
        return {"error": "aiohttp not available for HA API calls"}

    unique_id_prefix = f"ups_unified_{device_identity}_"
    deleted_entities: list[str] = []

    try:
        async with aiohttp.ClientSession() as session:
            ws_url = _build_ha_ws_url(ha_url)
            timeout = aiohttp.ClientTimeout(total=15)
            async with session.ws_connect(ws_url, timeout=timeout) as ws:
                await ws.receive_json()  # auth_required
                await ws.send_json({"type": "auth", "access_token": ha_token})
                auth = await ws.receive_json()
                if auth.get("type") != "auth_ok":
                    return {"error": "HA websocket authentication failed"}

                await ws.send_json({"id": 1, "type": "config/entity_registry/list"})
                response = await _ws_recv_result(ws, 1)
                if not response.get("success", False):
                    return {"error": "HA entity registry list failed"}

                registry_entries = response.get("result", [])
                matching_entities = [
                    entry.get("entity_id")
                    for entry in registry_entries
                    if str(entry.get("unique_id", "")).startswith(unique_id_prefix)
                ]

                LOG.debug(
                    "Found %d entities matching unique_id prefix %s",
                    len(matching_entities),
                    unique_id_prefix,
                )

                command_id = 10
                for entity_id in matching_entities:
                    if not entity_id:
                        continue
                    await ws.send_json(
                        {
                            "id": command_id,
                            "type": "config/entity_registry/remove",
                            "entity_id": entity_id,
                        }
                    )
                    remove_resp = await _ws_recv_result(ws, command_id)
                    if remove_resp.get("success", False):
                        deleted_entities.append(entity_id)
                        LOG.debug("Removed HA entity registry entry: %s", entity_id)
                    else:
                        LOG.warning(
                            "Failed to remove HA entity %s: %s",
                            entity_id,
                            remove_resp.get("error"),
                        )
                    command_id += 1

        LOG.info(
            "Deleted %d entity registry entries for identity %s",
            len(deleted_entities),
            device_identity,
        )
        return {"deleted": deleted_entities, "method": "entity_registry"}

    except asyncio.TimeoutError:
        return {"error": "Timeout connecting to Home Assistant"}
    except Exception as err:  # grain: ignore NAKED_EXCEPT
        LOG.exception("HA entity deletion failed: %s", err)
        return {"error": str(err)}


async def apply_entity_default_states(
    ha_url: str,
    ha_token: str,
    device_identity: str,
    expected_defaults: dict[str, bool],
) -> dict[str, Any]:
    """Apply default enabled/disabled states in HA entity registry.

    For non-core entities, use disabled_by="user". For core entities, clear
    disabled_by so they are enabled.
    """
    if not ha_url or not ha_token:
        return {"skipped": True, "reason": "HA URL or token not configured"}

    if aiohttp is None:
        return {"error": "aiohttp not available for HA API calls"}

    unique_id_prefix = f"ups_unified_{device_identity}_"
    updated: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    try:
        async with aiohttp.ClientSession() as session:
            ws_url = _build_ha_ws_url(ha_url)
            timeout = aiohttp.ClientTimeout(total=20)
            async with session.ws_connect(ws_url, timeout=timeout) as ws:
                await ws.receive_json()  # auth_required
                await ws.send_json({"type": "auth", "access_token": ha_token})
                auth = await ws.receive_json()
                if auth.get("type") != "auth_ok":
                    return {"error": "HA websocket authentication failed"}

                # Give HA a short window to ingest republished discovery entries.
                registry_entries: list[dict[str, Any]] = []
                expected_count = len(expected_defaults)
                request_id = 1
                for _ in range(12):
                    await ws.send_json(
                        {"id": request_id, "type": "config/entity_registry/list"}
                    )
                    response = await _ws_recv_result(ws, request_id)
                    if not response.get("success", False):
                        return {
                            "error": "HA entity registry list failed",
                            "details": response.get("error"),
                        }
                    registry_entries = response.get("result", [])
                    matching = [
                        entry
                        for entry in registry_entries
                        if str(entry.get("unique_id", "")).startswith(unique_id_prefix)
                    ]
                    if len(matching) >= expected_count:
                        break
                    await asyncio.sleep(0.5)
                    request_id += 1

                matching = [
                    entry
                    for entry in registry_entries
                    if str(entry.get("unique_id", "")).startswith(unique_id_prefix)
                ]

                command_id = 100
                for entry in matching:
                    unique_id = str(entry.get("unique_id", ""))
                    entity_id = entry.get("entity_id")
                    if not entity_id or not unique_id.startswith(unique_id_prefix):
                        continue
                    metric_key = unique_id[len(unique_id_prefix) :]
                    if metric_key not in expected_defaults:
                        continue
                    should_enable = expected_defaults[metric_key]
                    desired_disabled_by = None if should_enable else "user"
                    if entry.get("disabled_by") == desired_disabled_by:
                        continue

                    await ws.send_json(
                        {
                            "id": command_id,
                            "type": "config/entity_registry/update",
                            "entity_id": entity_id,
                            "disabled_by": desired_disabled_by,
                        }
                    )
                    update_resp = await _ws_recv_result(ws, command_id)
                    if update_resp.get("success", False):
                        updated.append(
                            {
                                "entity_id": entity_id,
                                "metric_key": metric_key,
                                "disabled_by": desired_disabled_by,
                            }
                        )
                    else:
                        failed.append(
                            {
                                "entity_id": entity_id,
                                "metric_key": metric_key,
                                "error": update_resp.get("error"),
                            }
                        )
                    command_id += 1

        return {
            "updated": updated,
            "failed": failed,
            "expected_count": len(expected_defaults),
        }
    except asyncio.TimeoutError:
        return {"error": "Timeout connecting to Home Assistant"}
    except Exception as err:  # grain: ignore NAKED_EXCEPT
        LOG.exception("HA entity default apply failed: %s", err)
        return {"error": str(err)}


async def delete_stale_ups_entities(
    ha_url: str,
    ha_token: str,
    expected_unique_ids: set[str],
    expected_device_identifiers: set[str] | None = None,
) -> dict[str, Any]:
    """Delete stale ups2mqtt entities from Home Assistant entity registry.

    Any entity with `unique_id` starting with `ups_unified_` that is not present
    in `expected_unique_ids` will be removed.
    """
    if not ha_url or not ha_token:
        return {"skipped": True, "reason": "HA URL or token not configured"}

    if aiohttp is None:
        return {"error": "aiohttp not available for HA API calls"}

    stale_entities: list[tuple[str, str]] = []
    deleted_entities: list[str] = []
    removed_devices: list[str] = []

    try:
        async with aiohttp.ClientSession() as session:
            ws_url = _build_ha_ws_url(ha_url)
            timeout = aiohttp.ClientTimeout(total=20)
            async with session.ws_connect(ws_url, timeout=timeout) as ws:
                await ws.receive_json()  # auth_required
                await ws.send_json({"type": "auth", "access_token": ha_token})
                auth = await ws.receive_json()
                if auth.get("type") != "auth_ok":
                    return {"error": "HA websocket authentication failed"}

                await ws.send_json({"id": 1, "type": "config/entity_registry/list"})
                response = await _ws_recv_result(ws, 1)
                if not response.get("success", False):
                    return {"error": "HA entity registry list failed"}

                registry_entries = response.get("result", [])
                for entry in registry_entries:
                    unique_id = str(entry.get("unique_id", ""))
                    entity_id = str(entry.get("entity_id", "")).strip()
                    if (
                        unique_id.startswith("ups_unified_")
                        and unique_id not in expected_unique_ids
                        and entity_id
                    ):
                        stale_entities.append((entity_id, unique_id))

                command_id = 100
                for entity_id, unique_id in stale_entities:
                    await ws.send_json(
                        {
                            "id": command_id,
                            "type": "config/entity_registry/remove",
                            "entity_id": entity_id,
                        }
                    )
                    remove_resp = await _ws_recv_result(ws, command_id)
                    if remove_resp.get("success", False):
                        deleted_entities.append(entity_id)
                        LOG.debug(
                            "Removed stale HA entity %s (unique_id=%s)",
                            entity_id,
                            unique_id,
                        )
                    else:
                        LOG.warning(
                            "Failed to remove stale HA entity %s (unique_id=%s): %s",
                            entity_id,
                            unique_id,
                            remove_resp.get("error"),
                        )
                    command_id += 1

                # Also remove stale/orphan ups2mqtt device-registry entries.
                # These can persist after entities are deleted and inflate HA device count.
                if expected_device_identifiers is None:
                    expected_device_identifiers = {"ups_unified_bridge"}

                await ws.send_json({"id": 2, "type": "config/entity_registry/list"})
                current_entities_resp = await _ws_recv_result(ws, 2)
                if not current_entities_resp.get("success", False):
                    return {"error": "HA entity registry list failed after prune"}
                current_entities = current_entities_resp.get("result", [])
                ups_entities_by_device: dict[str, int] = {}
                for entry in current_entities:
                    unique_id = str(entry.get("unique_id", ""))
                    device_id = str(entry.get("device_id", "")).strip()
                    if unique_id.startswith("ups_unified_") and device_id:
                        ups_entities_by_device[device_id] = (
                            ups_entities_by_device.get(device_id, 0) + 1
                        )

                await ws.send_json({"id": 3, "type": "config/device_registry/list"})
                device_registry_resp = await _ws_recv_result(ws, 3)
                if not device_registry_resp.get("success", False):
                    return {"error": "HA device registry list failed"}
                device_registry = device_registry_resp.get("result", [])

                command_id = 200
                for device in device_registry:
                    device_id = str(device.get("id", "")).strip()
                    if not device_id:
                        continue
                    identifiers = device.get("identifiers") or []
                    mqtt_ids = [
                        str(item[1])
                        for item in identifiers
                        if isinstance(item, (list, tuple))
                        and len(item) == 2
                        and item[0] == "mqtt"
                    ]
                    ups_ids = [
                        item for item in mqtt_ids if item.startswith("ups_unified_")
                    ]
                    if not ups_ids:
                        continue
                    if any(item in expected_device_identifiers for item in ups_ids):
                        continue
                    if ups_entities_by_device.get(device_id, 0) > 0:
                        continue

                    for config_entry_id in device.get("config_entries") or []:
                        if not config_entry_id:
                            continue
                        await ws.send_json(
                            {
                                "id": command_id,
                                "type": "config/device_registry/remove_config_entry",
                                "device_id": device_id,
                                "config_entry_id": config_entry_id,
                            }
                        )
                        remove_device_resp = await _ws_recv_result(ws, command_id)
                        if remove_device_resp.get("success", False):
                            removed_devices.append(device_id)
                        else:
                            LOG.warning(
                                "Failed to remove stale HA device %s: %s",
                                device_id,
                                remove_device_resp.get("error"),
                            )
                        command_id += 1

        return {
            "deleted": deleted_entities,
            "removed_devices": removed_devices,
            "scanned": len(stale_entities),
            "expected": len(expected_unique_ids),
        }
    except asyncio.TimeoutError:
        return {"error": "Timeout connecting to Home Assistant"}
    except Exception as err:  # grain: ignore NAKED_EXCEPT
        LOG.exception("HA stale entity prune failed: %s", err)
        return {"error": str(err)}
