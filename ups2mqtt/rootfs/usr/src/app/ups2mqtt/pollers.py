# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import shlex
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from pymodbus.client import ModbusTcpClient

from .capability_repository import get_capability_repository
from .model import DeviceConfig

LOG = logging.getLogger("ups2mqtt.pollers")
_HYBRID_COLLISION_LOGGED: set[str] = set()

METADATA_REFRESH_INTERVAL_SECONDS = 3600
IDLE_RECONNECT_SECONDS = 300.0
_POLL_TIMING_LOCK = threading.Lock()

SMARTUPS_OID_MODEL = "1.3.6.1.4.1.318.1.1.1.1.1.1.0"
SMARTUPS_OID_LOCATION = "1.3.6.1.4.1.318.1.1.1.1.1.2.0"
SMARTUPS_OID_SERIAL = "1.3.6.1.4.1.318.1.1.1.1.2.3.0"
SMARTUPS_OID_FIRMWARE = "1.3.6.1.4.1.318.1.1.1.1.2.1.0"
SMARTUPS_OID_FIRMWARE_DATE = "1.3.6.1.4.1.318.1.1.1.1.2.2.0"
RACKPDU_OID_MODEL = "1.3.6.1.4.1.318.1.1.12.1.5.0"
RACKPDU_OID_SERIAL = "1.3.6.1.4.1.318.1.1.12.1.6.0"
RACKPDU_OID_FIRMWARE = "1.3.6.1.4.1.318.1.1.12.1.3.0"
RACKPDU_OID_FIRMWARE_DATE = "1.3.6.1.4.1.318.1.1.12.1.4.0"
UIO_SENSOR_STATUS_TEMP_C_BASE = "1.3.6.1.4.1.318.1.1.25.1.2.1.6"
UIO_SENSOR_STATUS_HUMIDITY_BASE = "1.3.6.1.4.1.318.1.1.25.1.2.1.7"
SMARTUPS_OID_INPUT_FREQUENCY = "1.3.6.1.4.1.318.1.1.1.3.2.4.0"
UPS_MIB_OID_INPUT_FREQUENCY_LINE1 = "1.3.6.1.2.1.33.1.3.3.1.2.1"
UPS_MIB_OID_MANUFACTURER = "1.3.6.1.2.1.33.1.1.1.0"
UPS_MIB_OID_MODEL = "1.3.6.1.2.1.33.1.1.2.0"
UPS_MIB_OID_FIRMWARE = "1.3.6.1.2.1.33.1.1.3.0"
UPS_MIB_OID_NAME = "1.3.6.1.2.1.33.1.1.5.0"

CYBERPOWER_OID_MODEL = "1.3.6.1.4.1.3808.1.1.1.1.1.1.0"
CYBERPOWER_OID_SERIAL = "1.3.6.1.4.1.3808.1.1.1.1.2.3.0"
CYBERPOWER_OID_FIRMWARE = "1.3.6.1.4.1.3808.1.1.1.1.2.4.0"


@dataclass(slots=True)
class _EndpointSession:
    lock: threading.Lock = field(default_factory=threading.Lock)
    client: ModbusTcpClient | None = None
    resolved_unit_param: str | None = None
    reconnect_count: int = 0
    recreate_count: int = 0
    last_io_monotonic: float = 0.0


@dataclass(slots=True)
class _ApcSnmpCache:
    metadata: dict[str, str] = field(default_factory=dict)
    detection: dict[str, str | None] = field(default_factory=dict)
    last_refresh_monotonic: float = 0.0


@dataclass(slots=True)
class _CyberPowerSnmpCache:
    metadata: dict[str, str] = field(default_factory=dict)
    last_refresh_monotonic: float = 0.0


@dataclass(slots=True)
class _UpsMibSnmpCache:
    metadata: dict[str, str] = field(default_factory=dict)
    last_refresh_monotonic: float = 0.0


_MODBUS_SESSIONS_LOCK = threading.Lock()
_MODBUS_SESSIONS: dict[str, _EndpointSession] = {}
_APC_SNMP_CACHE_LOCK = threading.Lock()
_APC_SNMP_CACHE: dict[str, _ApcSnmpCache] = {}
_CYBERPOWER_SNMP_CACHE_LOCK = threading.Lock()
_CYBERPOWER_SNMP_CACHE: dict[str, _CyberPowerSnmpCache] = {}
_UPS_MIB_SNMP_CACHE_LOCK = threading.Lock()
_UPS_MIB_SNMP_CACHE: dict[str, _UpsMibSnmpCache] = {}
_CATALOG_CACHE_LOCK = threading.Lock()
_CATALOG_SPECS_CACHE: dict[str, list[dict[str, Any]]] = {}
_CATALOG_KEYS_CACHE: dict[tuple[str, str, bool], tuple[set[str], set[str]]] = {}
_CATALOG_ALIAS_CACHE: dict[tuple[str, str, bool], dict[str, str]] = {}


def get_metadata_refresh_interval_seconds() -> int:
    with _POLL_TIMING_LOCK:
        return int(METADATA_REFRESH_INTERVAL_SECONDS)


def set_metadata_refresh_interval_seconds(seconds: int) -> None:
    if int(seconds) <= 0:
        raise ValueError("metadata refresh interval must be > 0 seconds")
    global METADATA_REFRESH_INTERVAL_SECONDS
    with _POLL_TIMING_LOCK:
        METADATA_REFRESH_INTERVAL_SECONDS = int(seconds)


def get_idle_reconnect_seconds() -> float:
    with _POLL_TIMING_LOCK:
        return float(IDLE_RECONNECT_SECONDS)


def set_idle_reconnect_seconds(seconds: float) -> None:
    if float(seconds) <= 0:
        raise ValueError("idle reconnect interval must be > 0 seconds")
    global IDLE_RECONNECT_SECONDS
    with _POLL_TIMING_LOCK:
        IDLE_RECONNECT_SECONDS = float(seconds)


def clear_catalog_poll_cache() -> None:
    """Clear cached catalog lookups used by the poll selection path."""
    with _CATALOG_CACHE_LOCK:
        _CATALOG_SPECS_CACHE.clear()
        _CATALOG_KEYS_CACHE.clear()
        _CATALOG_ALIAS_CACHE.clear()


def _decode_registers(
    registers: list[int], descriptor: dict[str, Any]
) -> int | float | str | None:
    dtype = str(descriptor.get("type", "uint16"))
    scale = descriptor.get("scale", 1)
    raw: int | None = None

    if dtype in {"uint16", "int16"} and registers:
        raw = int(registers[0])
        if dtype == "int16" and raw >= 0x8000:
            raw -= 0x10000
    elif dtype in {"uint32", "int32"} and len(registers) >= 2:
        word_order = str(descriptor.get("word_order", "big")).lower()
        if word_order == "little":
            raw = (int(registers[1]) << 16) | int(registers[0])
        else:
            raw = (int(registers[0]) << 16) | int(registers[1])
        if dtype == "int32" and raw >= 0x80000000:
            raw -= 0x100000000
    elif dtype == "ascii" and registers:
        chars: list[str] = []
        for reg in registers:
            hi = (reg >> 8) & 0xFF
            lo = reg & 0xFF
            if hi:
                chars.append(chr(hi))
            if lo:
                chars.append(chr(lo))
        return "".join(chars).strip() or None
    else:
        return None

    if raw is None:
        return None
    if isinstance(scale, (int, float)) and scale not in (0, 1):
        return raw / float(scale)
    return raw


def _get_modbus_session(endpoint_key: str) -> _EndpointSession:
    with _MODBUS_SESSIONS_LOCK:
        return _MODBUS_SESSIONS.setdefault(endpoint_key, _EndpointSession())


def _get_read_param_names(client: ModbusTcpClient) -> list[str]:
    params = inspect.signature(client.read_holding_registers).parameters
    return [name for name in ("device_id", "slave", "unit") if name in params]


def _read_holding_registers(
    session: _EndpointSession,
    address: int,
    count: int,
    unit_id: int,
):
    client = session.client
    if client is None:
        raise ConnectionError("Modbus client not initialized")

    read_fn = client.read_holding_registers
    param_names = _get_read_param_names(client)

    attempts: list[tuple[str, str | int | None]] = []
    if session.resolved_unit_param:
        attempts.append(("kw", session.resolved_unit_param))
    for candidate in param_names:
        if candidate != session.resolved_unit_param:
            attempts.append(("kw", candidate))
    attempts.extend(
        [
            ("positional", unit_id),
            ("none", None),
        ]
    )

    last_type_error: TypeError | None = None
    for kind, value in attempts:
        try:
            if kind == "kw":
                result = read_fn(address, count=count, **{str(value): unit_id})
                session.resolved_unit_param = str(value)
                return result
            if kind == "positional":
                return read_fn(address, count, int(value))
            if "count" in inspect.signature(read_fn).parameters:
                return read_fn(address, count=count)
            return read_fn(address, count)
        except TypeError as err:
            last_type_error = err
            continue

    if last_type_error is not None:
        raise last_type_error
    raise TypeError("No compatible pymodbus read_holding_registers signature found")


def _is_error_response(result: Any) -> bool:
    if hasattr(result, "isError") and callable(result.isError):
        return bool(result.isError())
    if hasattr(result, "is_error") and callable(result.is_error):
        return bool(result.is_error())
    if not hasattr(result, "registers"):
        return True
    if getattr(result, "registers", None) is None:
        return True
    return False


def _mark_io(session: _EndpointSession) -> None:
    session.last_io_monotonic = time.monotonic()


def _ensure_session_client(session: _EndpointSession, device: DeviceConfig) -> None:
    if session.client is None:
        session.client = ModbusTcpClient(host=device.host, port=device.port)
        session.resolved_unit_param = None


def _close_session_client(session: _EndpointSession) -> None:
    if session.client is None:
        return
    try:
        session.client.close()
    except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
        pass
    # Always drop the client object after close so reconnects rebuild a fresh
    # transaction context. Reusing a timed-out pymodbus client can lead to
    # misaligned/stale responses being associated with subsequent reads.
    session.client = None
    session.resolved_unit_param = None


def _recreate_session_client(session: _EndpointSession, device: DeviceConfig) -> None:
    _close_session_client(session)
    session.client = ModbusTcpClient(host=device.host, port=device.port)
    session.resolved_unit_param = None


def _async_connection_error_like(err: Exception) -> bool:
    text = str(err).lower()
    if "broken pipe" in text or "connection" in text or "reset" in text:
        return True
    if "timed out" in text or "timeout" in text:
        return True
    return isinstance(err, (OSError, TimeoutError, ConnectionError))


def _reconnect_session(
    session: _EndpointSession,
    device: DeviceConfig,
    *,
    reason: str,
    recreate_client: bool,
) -> bool:
    session.reconnect_count += 1
    if recreate_client:
        session.recreate_count += 1
        _recreate_session_client(session, device)
    else:
        _close_session_client(session)

    _ensure_session_client(session, device)
    assert session.client is not None
    started = time.monotonic()
    ok = bool(session.client.connect())
    LOG.debug(
        "[%s:%s] reconnect(reason=%s recreate=%s) -> %s (%.3fs total_reconnects=%d total_recreates=%d)",
        device.host,
        device.port,
        reason,
        recreate_client,
        ok,
        time.monotonic() - started,
        session.reconnect_count,
        session.recreate_count,
    )
    if ok:
        _mark_io(session)
    return ok


def _ensure_connected(session: _EndpointSession, device: DeviceConfig) -> bool:
    _ensure_session_client(session, device)
    assert session.client is not None

    if device.keep_connection_open and session.last_io_monotonic > 0:
        idle_for = time.monotonic() - session.last_io_monotonic
        if idle_for >= IDLE_RECONNECT_SECONDS:
            LOG.info(
                "[%s] Modbus socket idle for %.1fs; reconnecting before poll",
                device.id,
                idle_for,
            )
            reconnected = _reconnect_session(
                session,
                device,
                reason=f"idle>{IDLE_RECONNECT_SECONDS:.0f}s",
                recreate_client=False,
            )
            if not reconnected:
                reconnected = _reconnect_session(
                    session,
                    device,
                    reason="idle_reconnect_retry",
                    recreate_client=True,
                )
            return reconnected

    try:
        if bool(getattr(session.client, "connected", False)):
            return True
        ok = bool(session.client.connect())
        if ok:
            _mark_io(session)
        return ok
    except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
        LOG.debug("[%s] Connection attempt failed: %s", device.id, err)
        return False


def _try_block_reads(
    session: _EndpointSession,
    device: DeviceConfig,
    descriptors: list[dict[str, Any]],
    register_blocks: list[dict[str, Any]],
    output: dict[str, Any],
    decoded: set[str],
) -> None:
    for block in register_blocks:
        start = block.get("start_address")
        count = block.get("count")
        if start is None or count is None:
            continue

        start = int(start)
        count = int(count)
        if count <= 0:
            continue

        result = None
        try:
            result = _read_holding_registers(session, start, count, device.unit_id)
            _mark_io(session)
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            if not _async_connection_error_like(err):
                continue
            recreate_for_socket_error = (
                "broken pipe" in str(err).lower() or "reset" in str(err).lower()
            )
            reconnected = _reconnect_session(
                session,
                device,
                reason=f"block:{block.get('name', start)}:{type(err).__name__}",
                recreate_client=recreate_for_socket_error,
            )
            if not reconnected and not recreate_for_socket_error:
                reconnected = _reconnect_session(
                    session,
                    device,
                    reason=f"block:{block.get('name', start)}:retry_recreate",
                    recreate_client=True,
                )
            if not reconnected:
                continue
            try:
                result = _read_holding_registers(session, start, count, device.unit_id)
                _mark_io(session)
            except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                continue

        if result is None or _is_error_response(result):
            continue

        regs = list(getattr(result, "registers", []) or [])
        if not regs:
            continue

        for descriptor in descriptors:
            key = str(descriptor["key"])
            if key in decoded:
                continue
            address = int(descriptor["address"])
            reg_count = int(descriptor.get("count", 1))
            offset = address - start
            if offset < 0 or offset + reg_count > len(regs):
                continue
            value = _decode_registers(regs[offset : offset + reg_count], descriptor)
            if value is not None:
                output[key] = value
                decoded.add(key)


def _block_intersects_descriptors(
    block: dict[str, Any], descriptors: list[dict[str, Any]]
) -> bool:
    start_raw = block.get("start_address")
    count_raw = block.get("count")
    if start_raw is None or count_raw is None:
        return False
    try:
        block_start = int(start_raw)
        block_count = int(count_raw)
    except (TypeError, ValueError):
        return False
    if block_count <= 0:
        return False
    block_end = block_start + block_count

    for descriptor in descriptors:
        try:
            address = int(descriptor["address"])
            reg_count = int(descriptor.get("count", 1))
        except (KeyError, TypeError, ValueError):
            continue
        if reg_count <= 0:
            continue
        desc_end = address + reg_count
        if address < block_end and desc_end > block_start:
            return True
    return False


def _try_individual_reads(
    session: _EndpointSession,
    device: DeviceConfig,
    descriptors: list[dict[str, Any]],
    output: dict[str, Any],
    decoded: set[str],
) -> str | None:
    # Fail fast within one polling cycle when a device starts returning repeated
    # timeout/exception responses on individual fallback reads.
    max_failures_per_cycle = 3
    failures = 0

    for descriptor in descriptors:
        if failures >= max_failures_per_cycle:
            warning = (
                f"Aborted remaining individual Modbus reads after {failures} failures in this cycle"
            )
            LOG.warning("[%s] %s", device.id, warning)
            return warning

        key = str(descriptor["key"])
        if key in decoded:
            continue

        address = int(descriptor["address"])
        reg_count = int(descriptor.get("count", 1))
        result = None
        try:
            result = _read_holding_registers(
                session, address, reg_count, device.unit_id
            )
            _mark_io(session)
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            if not _async_connection_error_like(err):
                failures += 1
                continue
            recreate_for_socket_error = (
                "broken pipe" in str(err).lower() or "reset" in str(err).lower()
            )
            reconnected = _reconnect_session(
                session,
                device,
                reason=f"register:{key}:{type(err).__name__}",
                recreate_client=recreate_for_socket_error,
            )
            if not reconnected and not recreate_for_socket_error:
                reconnected = _reconnect_session(
                    session,
                    device,
                    reason=f"register:{key}:retry_recreate",
                    recreate_client=True,
                )
            if not reconnected:
                failures += 1
                continue
            try:
                result = _read_holding_registers(
                    session,
                    address,
                    reg_count,
                    device.unit_id,
                )
                _mark_io(session)
            except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                failures += 1
                continue

        if result is None or _is_error_response(result):
            failures += 1
            continue

        regs = list(getattr(result, "registers", []) or [])
        value = _decode_registers(regs, descriptor)
        if value is not None:
            output[key] = value
            decoded.add(key)

    return None


def _poll_modbus_sync(
    device: DeviceConfig,
    profile: dict[str, Any],
    poll_groups: set[str] | None = None,
    *,
    suppress_runtime_metadata_merge: bool = False,
) -> dict[str, Any]:
    output: dict[str, Any] = {}

    # Contract: planning is driven by tier-enabled DB catalog mappings.
    base_registers = _filter_modbus_registers_by_catalog(device, profile)

    descriptors = [
        item for item in base_registers if isinstance(item, dict) and item.get("key")
    ]
    register_blocks = [
        item for item in profile.get("register_blocks", []) if isinstance(item, dict)
    ]
    allowed_groups = poll_groups or {"slow"}
    descriptors = [
        item
        for item in descriptors
        if str(item.get("poll_group", "slow")) in allowed_groups
    ]
    # Keep block transport reads group-agnostic, then prune to only blocks that
    # intersect descriptors required for this cycle.
    register_blocks = [
        item
        for item in register_blocks
        if _block_intersects_descriptors(item, descriptors)
    ]
    if not descriptors:
        return output

    endpoint_key = f"{device.host}:{device.port}"
    session = _get_modbus_session(endpoint_key)

    poll_started = time.monotonic()
    lock_wait = 0.0
    ensure_connection_elapsed = 0.0
    block_reads_elapsed = 0.0
    individual_reads_elapsed = 0.0
    close_elapsed = 0.0
    modbus_elapsed = 0.0
    snmp_metadata_elapsed = 0.0
    snmp_external_elapsed = 0.0
    reconnects_at_start = session.reconnect_count
    recreates_at_start = session.recreate_count

    decoded: set[str] = set()

    lock_started = time.monotonic()
    with session.lock:
        lock_wait = time.monotonic() - lock_started
        modbus_started = time.monotonic()

        ensure_connection_started = time.monotonic()
        if not _ensure_connected(session, device):
            raise ConnectionError(
                f"Modbus connect failed for {device.host}:{device.port}"
            )
        ensure_connection_elapsed = time.monotonic() - ensure_connection_started

        block_reads_started = time.monotonic()
        _try_block_reads(
            session,
            device,
            descriptors,
            register_blocks,
            output,
            decoded,
        )
        block_reads_elapsed = time.monotonic() - block_reads_started

        individual_reads_started = time.monotonic()
        cycle_warning = _try_individual_reads(
            session, device, descriptors, output, decoded
        )
        individual_reads_elapsed = time.monotonic() - individual_reads_started

        if not device.keep_connection_open:
            close_started = time.monotonic()
            _close_session_client(session)
            session.last_io_monotonic = 0.0
            close_elapsed = time.monotonic() - close_started

        modbus_elapsed = time.monotonic() - modbus_started

    # Bridge raw transport keys to canonical catalog keys
    # (e.g. utility_voltage -> input_voltage).
    alias_map = _catalog_alias_to_canonical_map(device, transport="modbus")
    if alias_map:
        _promote_alias_values(output, alias_map)

    # For multi-source protocol drivers, skip automatic SNMP metadata merge.
    # Multi-source drivers handle metadata independently through their own runtime metadata path.
    protocol = profile.get("protocol")
    is_multi_source = protocol == "multi_source"
    skip_runtime_metadata_merge = suppress_runtime_metadata_merge or is_multi_source

    if device.source.startswith("apc_modbus") and not skip_runtime_metadata_merge:
        snmp_metadata_started = time.monotonic()
        cache = _maybe_refresh_apc_snmp_metadata(device)
        snmp_metadata_elapsed = time.monotonic() - snmp_metadata_started
        _merge_apc_device_metadata(output, cache.metadata)

        snmp_external_started = time.monotonic()
        _merge_apc_external_probe_data(device, output, cache.detection)
        snmp_external_elapsed = time.monotonic() - snmp_external_started
    elif (
        device.source.startswith("cyberpower_modbus")
        and not skip_runtime_metadata_merge
    ):
        snmp_metadata_started = time.monotonic()
        # Get SNMP fields from catalog (respects tier gating)
        snmp_oid_map = _metadata_snmp_oid_map(device)
        cache = _maybe_refresh_cyberpower_snmp_metadata(device, snmp_oid_map)
        snmp_metadata_elapsed = time.monotonic() - snmp_metadata_started
        _merge_cyberpower_device_metadata(output, cache.metadata)

    if cycle_warning:
        output["__poll_warning__"] = cycle_warning

    LOG.info(
        "[%s] Poll timing breakdown: total=%.3fs, lock_wait=%.3fs, modbus=%.3fs, "
        "connect=%.3fs, block_reads=%.3fs, individual_reads=%.3fs, close=%.3fs, "
        "snmp_metadata=%.3fs, snmp_external=%.3fs, reconnects=%d, recreates=%d",
        device.id,
        time.monotonic() - poll_started,
        lock_wait,
        modbus_elapsed,
        ensure_connection_elapsed,
        block_reads_elapsed,
        individual_reads_elapsed,
        close_elapsed,
        snmp_metadata_elapsed,
        snmp_external_elapsed,
        session.reconnect_count - reconnects_at_start,
        session.recreate_count - recreates_at_start,
    )

    return output


def _snmp_get_sync(host: str, community: str, oid: str, *, port: int = 161) -> str | None:
    try:
        from pysnmp.hlapi import (  # type: ignore[attr-defined]
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            getCmd,
        )

        iterator = getCmd(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            UdpTransportTarget((host, port), timeout=2, retries=0),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
        error_indication, error_status, _error_index, var_binds = next(iterator)
        if error_indication or error_status:
            return None
        for _oid, value in var_binds:
            text = str(value)
            return text if text else None
        return None
    except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
        return _snmp_get_sync_v1arch(host, community, oid, port=port)


def _snmp_get_sync_v1arch(
    host: str, community: str, oid: str, *, port: int = 161
) -> str | None:
    from pysnmp.hlapi.v1arch.asyncio import (  # type: ignore[attr-defined]
        CommunityData,
        ObjectIdentity,
        ObjectType,
        SnmpDispatcher,
        UdpTransportTarget,
        get_cmd,
    )

    async def _run() -> str | None:
        target = await UdpTransportTarget.create((host, port), timeout=2, retries=0)
        dispatcher = SnmpDispatcher()
        error_indication, error_status, _error_index, var_binds = await get_cmd(
            dispatcher,
            CommunityData(community),
            target,
            ObjectType(ObjectIdentity(oid)),
        )
        if error_indication or error_status:
            return None
        for _oid, value in var_binds:
            text = str(value)
            return text if text else None
        return None

    # Fallback path may be invoked from async call chains (e.g. metadata refresh
    # during multi_source polling). asyncio.run() cannot execute inside an already
    # running event loop, so offload to a worker thread in that case.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    return concurrent.futures.ThreadPoolExecutor(max_workers=1).submit(
        lambda: asyncio.run(_run())
    ).result()


def _snmp_get_first(
    host: str, community: str, oids: list[str], *, port: int = 161
) -> str | None:
    for oid in oids:
        raw = _snmp_get_sync(host, community, oid, port=port)
        if raw is not None:
            return raw
    return None


def _parse_external_temp_c(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value > 120:
        return value / 10.0
    return float(value)


def _parse_external_humidity_pct(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value > 100:
        return value / 10.0
    return float(value)


def _parse_frequency_hz(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 400:
        return value / 10.0
    return value


def _apc_cache_key(device: DeviceConfig) -> str:
    return f"{device.source}|{device.host}|{device.snmp_port}|{device.snmp_community}"


def _default_apc_cache() -> _ApcSnmpCache:
    return _ApcSnmpCache(metadata={}, detection={}, last_refresh_monotonic=0.0)


def get_runtime_metadata(device: DeviceConfig) -> dict[str, str]:
    """Get runtime metadata from cache for discovery device info.

    Returns cached device identity metadata (manufacturer, model, serial, firmware)
    that was fetched independently from sensor poll results.

    This allows discovery device blocks to populate even when metadata fields
    are not enabled as regular sensors in the profile.

    Field Normalization:
    - Ensures both "sw_version" and "firmware_version" are available
    - APC drivers store firmware under both names
    - CyberPower drivers store as "sw_version" with firmware_version alias
    - This ensures HA device blocks always have sw_version field
    """
    metadata: dict[str, str] = {}

    if device.source.startswith("apc_modbus") or device.source == "ups_snmp_apc_mib":
        key = _apc_cache_key(device)
        with _APC_SNMP_CACHE_LOCK:
            cache = _APC_SNMP_CACHE.get(key)
            if cache:
                metadata.update(cache.metadata)
                # Normalize: ensure sw_version available from firmware_version
                # (APC cache stores both, but ensure for backward compat)
                if "firmware_version" in metadata and "sw_version" not in metadata:
                    metadata["sw_version"] = metadata["firmware_version"]
    elif device.source.startswith("cyberpower_modbus"):
        key = (
            f"{device.source}|{device.host}|{device.snmp_port}|"
            f"{device.snmp_community}"
        )
        with _CYBERPOWER_SNMP_CACHE_LOCK:
            cache = _CYBERPOWER_SNMP_CACHE.get(key)
            if cache:
                metadata.update(cache.metadata)
                # Normalize: ensure firmware_version available from sw_version
                # (CyberPower stores as sw_version, ensure firmware_version alias)
                if "sw_version" in metadata and "firmware_version" not in metadata:
                    metadata["firmware_version"] = metadata["sw_version"]
    elif device.source == "ups_snmp_ups_mib":
        key = _ups_mib_cache_key(device)
        with _UPS_MIB_SNMP_CACHE_LOCK:
            cache = _UPS_MIB_SNMP_CACHE.get(key)
            if cache:
                metadata.update(cache.metadata)
                # Normalize metadata fields expected by HA discovery device blocks.
                if "firmware_version" in metadata and "sw_version" not in metadata:
                    metadata["sw_version"] = metadata["firmware_version"]
                if "firmware" in metadata and "sw_version" not in metadata:
                    metadata["sw_version"] = metadata["firmware"]
                if "sw_version" in metadata and "firmware_version" not in metadata:
                    metadata["firmware_version"] = metadata["sw_version"]

    return metadata


def _maybe_refresh_apc_snmp_metadata(device: DeviceConfig) -> _ApcSnmpCache:
    key = _apc_cache_key(device)
    now = time.monotonic()
    with _APC_SNMP_CACHE_LOCK:
        cache = _APC_SNMP_CACHE.get(key)
        if cache is None:
            cache = _default_apc_cache()
            _APC_SNMP_CACHE[key] = cache

        if (
            cache.last_refresh_monotonic > 0
            and (now - cache.last_refresh_monotonic) < METADATA_REFRESH_INTERVAL_SECONDS
        ):
            return cache

    if device.source == "apc_modbus_rack_pdu":
        model_oids = [RACKPDU_OID_MODEL]
        location_oids: list[str] = []
        serial_oids = [RACKPDU_OID_SERIAL]
        firmware_oids = [RACKPDU_OID_FIRMWARE]
        firmware_date_oids = [RACKPDU_OID_FIRMWARE_DATE]
    else:
        model_oids = [SMARTUPS_OID_MODEL]
        location_oids = [SMARTUPS_OID_LOCATION]
        serial_oids = [SMARTUPS_OID_SERIAL]
        firmware_oids = [SMARTUPS_OID_FIRMWARE]
        firmware_date_oids = [SMARTUPS_OID_FIRMWARE_DATE]

    metadata = {
        "manufacturer": "APC",
    }
    model = _snmp_get_first(
        device.host, device.snmp_community, model_oids, port=device.snmp_port
    )
    if model:
        metadata["model"] = model
    location = _snmp_get_first(
        device.host, device.snmp_community, location_oids, port=device.snmp_port
    )
    if location:
        metadata["location"] = location
    serial = _snmp_get_first(
        device.host, device.snmp_community, serial_oids, port=device.snmp_port
    )
    if serial:
        metadata["serial_number"] = serial
    firmware = _snmp_get_first(
        device.host, device.snmp_community, firmware_oids, port=device.snmp_port
    )
    if firmware:
        metadata["firmware_version"] = firmware
        metadata["firmware"] = firmware
        metadata["sw_version"] = firmware  # HA device block standard field
    firmware_date = _snmp_get_first(
        device.host,
        device.snmp_community,
        firmware_date_oids,
        port=device.snmp_port,
    )
    if firmware_date:
        metadata["firmware_date"] = firmware_date
        metadata["hw_version"] = firmware_date

    temp_1_oid = _first_detected_probe_oid(
        device,
        [f"{UIO_SENSOR_STATUS_TEMP_C_BASE}.1.1", f"{UIO_SENSOR_STATUS_TEMP_C_BASE}.1"],
        _parse_external_temp_c,
    )
    humidity_1_oid = _first_detected_probe_oid(
        device,
        [
            f"{UIO_SENSOR_STATUS_HUMIDITY_BASE}.1.1",
            f"{UIO_SENSOR_STATUS_HUMIDITY_BASE}.1",
        ],
        _parse_external_humidity_pct,
    )
    temp_2_oid = _first_detected_probe_oid(
        device,
        [f"{UIO_SENSOR_STATUS_TEMP_C_BASE}.2.1", f"{UIO_SENSOR_STATUS_TEMP_C_BASE}.2"],
        _parse_external_temp_c,
    )
    humidity_2_oid = _first_detected_probe_oid(
        device,
        [
            f"{UIO_SENSOR_STATUS_HUMIDITY_BASE}.2.1",
            f"{UIO_SENSOR_STATUS_HUMIDITY_BASE}.2",
        ],
        _parse_external_humidity_pct,
    )
    frequency_oid = _first_detected_probe_oid(
        device,
        [SMARTUPS_OID_INPUT_FREQUENCY, UPS_MIB_OID_INPUT_FREQUENCY_LINE1],
        _parse_frequency_hz,
    )
    detection = {
        "temp_1_oid": temp_1_oid,
        "humidity_1_oid": humidity_1_oid,
        "temp_2_oid": temp_2_oid,
        "humidity_2_oid": humidity_2_oid,
        "frequency_oid": frequency_oid,
    }

    with _APC_SNMP_CACHE_LOCK:
        refreshed = _ApcSnmpCache(
            metadata=metadata,
            detection=detection,
            last_refresh_monotonic=time.monotonic(),
        )
        _APC_SNMP_CACHE[key] = refreshed

    LOG.info(
        "[%s] SNMP probe detection (hourly): temp1=%s hum1=%s temp2=%s hum2=%s freq=%s",
        device.id,
        bool(detection.get("temp_1_oid")),
        bool(detection.get("humidity_1_oid")),
        bool(detection.get("temp_2_oid")),
        bool(detection.get("humidity_2_oid")),
        bool(detection.get("frequency_oid")),
    )

    return refreshed


def _cyberpower_cache_key(device: DeviceConfig) -> str:
    return f"{device.source}|{device.host}|{device.snmp_port}|{device.snmp_community}"


def _default_cyberpower_cache() -> _CyberPowerSnmpCache:
    return _CyberPowerSnmpCache(metadata={}, last_refresh_monotonic=0.0)


def _ups_mib_cache_key(device: DeviceConfig) -> str:
    return f"{device.source}|{device.host}|{device.snmp_port}|{device.snmp_community}"


def _default_ups_mib_cache() -> _UpsMibSnmpCache:
    return _UpsMibSnmpCache(metadata={}, last_refresh_monotonic=0.0)


def _catalog_sensor_specs(device: DeviceConfig) -> list[dict[str, Any]]:
    source = str(device.source).strip()
    if not source:
        return []
    with _CATALOG_CACHE_LOCK:
        cached = _CATALOG_SPECS_CACHE.get(source)
    if cached is not None:
        return cached
    repo = get_capability_repository()
    try:
        loaded = repo.load_catalog_sensor_specs(source)
        specs = loaded if isinstance(loaded, list) else []
        with _CATALOG_CACHE_LOCK:
            _CATALOG_SPECS_CACHE[source] = specs
        return specs
    except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
        return []


def _catalog_keys_for_transport(
    device: DeviceConfig,
    *,
    transport: str,
) -> tuple[set[str], set[str]]:
    source = str(device.source).strip()
    enable_extended = bool(getattr(device, "enable_extended_fields", False))
    cache_key = (source, transport, enable_extended)
    with _CATALOG_CACHE_LOCK:
        cached = _CATALOG_KEYS_CACHE.get(cache_key)
    if cached is not None:
        enabled_keys, all_keys = cached
        return set(enabled_keys), set(all_keys)
    specs = _catalog_sensor_specs(device)
    if not specs:
        return set(), set()

    all_keys: set[str] = set()
    enabled_keys: set[str] = set()
    for spec in specs:
        source = str(spec.get("source", "")).strip().lower()
        if source != transport:
            continue
        key = str(spec.get("key", "")).strip()
        aliases = spec.get("aliases", [])
        aliases_list = (
            [str(item).strip() for item in aliases if str(item).strip()]
            if isinstance(aliases, list)
            else []
        )
        names = {key, *aliases_list}
        names.discard("")
        if not names:
            continue
        all_keys.update(names)
        tier = str(spec.get("tier", "normalized")).strip().lower() or "normalized"
        if tier == "extended" and not enable_extended:
            continue
        enabled_keys.update(names)
    with _CATALOG_CACHE_LOCK:
        _CATALOG_KEYS_CACHE[cache_key] = (set(enabled_keys), set(all_keys))
    return enabled_keys, all_keys


def _catalog_alias_to_canonical_map(
    device: DeviceConfig,
    *,
    transport: str,
) -> dict[str, str]:
    """Build alias->canonical map for tier-enabled catalog fields."""
    source = str(device.source).strip()
    enable_extended = bool(getattr(device, "enable_extended_fields", False))
    cache_key = (source, transport, enable_extended)
    with _CATALOG_CACHE_LOCK:
        cached = _CATALOG_ALIAS_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    specs = _catalog_sensor_specs(device)
    if not specs:
        return {}

    out: dict[str, str] = {}
    for spec in specs:
        source = str(spec.get("source", "")).strip().lower()
        if source != transport:
            continue
        canonical = str(spec.get("key", "")).strip()
        if not canonical:
            continue
        tier = str(spec.get("tier", "normalized")).strip().lower() or "normalized"
        if tier == "extended" and not enable_extended:
            continue
        aliases = spec.get("aliases", [])
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            alias_key = str(alias).strip()
            if not alias_key or alias_key == canonical:
                continue
            out.setdefault(alias_key, canonical)
    with _CATALOG_CACHE_LOCK:
        _CATALOG_ALIAS_CACHE[cache_key] = dict(out)
    return out


def _promote_alias_values(values: dict[str, Any], alias_map: dict[str, str]) -> None:
    """Populate canonical keys from alias values when canonical key is absent."""
    for alias_key, canonical_key in alias_map.items():
        if canonical_key in values:
            continue
        if alias_key not in values:
            continue
        values[canonical_key] = values[alias_key]


def _filter_modbus_registers_by_catalog(
    device: DeviceConfig,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    all_registers = profile.get("registers", [])
    if not isinstance(all_registers, list):
        return []
    enabled_keys, all_catalog_keys = _catalog_keys_for_transport(device, transport="modbus")
    if not all_catalog_keys:
        return [item for item in all_registers if isinstance(item, dict)]

    out: list[dict[str, Any]] = []
    for item in all_registers:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        if key in enabled_keys or key not in all_catalog_keys or key.lower().endswith("_bf"):
            out.append(item)
    return out


def _filter_snmp_oids_by_catalog(
    device: DeviceConfig,
    profile: dict[str, Any],
) -> dict[str, Any]:
    all_oids = profile.get("oids", {})
    if not isinstance(all_oids, dict):
        return {}
    enabled_keys, all_catalog_keys = _catalog_keys_for_transport(device, transport="snmp")
    if not all_catalog_keys:
        return {
            str(key): spec
            for key, spec in all_oids.items()
            if isinstance(spec, dict)
        }

    out: dict[str, Any] = {}
    for key, spec in all_oids.items():
        metric_key = str(key).strip()
        if not isinstance(spec, dict) or not metric_key:
            continue
        if metric_key in enabled_keys or metric_key not in all_catalog_keys:
            out[metric_key] = spec
    return out


def _metadata_snmp_oid_map(device: DeviceConfig) -> dict[str, str]:
    specs = _catalog_sensor_specs(device)
    if not specs:
        return {
            "model": CYBERPOWER_OID_MODEL,
            "serial_number": CYBERPOWER_OID_SERIAL,
            "sw_version": CYBERPOWER_OID_FIRMWARE,
        }

    enable_extended = bool(getattr(device, "enable_extended_fields", False))
    metadata_candidates = {
        "model",
        "serial_number",
        "sw_version",
        "firmware_version",
        "battery_replace_date_snmp",
        "battery_replace_date",
    }
    out: dict[str, str] = {}
    for spec in specs:
        source = str(spec.get("source", "")).strip().lower()
        if source != "snmp":
            continue
        key = str(spec.get("key", "")).strip()
        if key not in metadata_candidates:
            continue
        tier = str(spec.get("tier", "normalized")).strip().lower() or "normalized"
        if tier == "extended" and not enable_extended:
            continue
        reference = str(spec.get("reference", "")).strip()
        if reference and "." in reference:
            out[key] = reference
    if out:
        return out
    return {
        "model": CYBERPOWER_OID_MODEL,
        "serial_number": CYBERPOWER_OID_SERIAL,
        "sw_version": CYBERPOWER_OID_FIRMWARE,
        "battery_replace_date_snmp": "1.3.6.1.4.1.3808.1.1.1.2.1.3.0",
    }


def _maybe_refresh_cyberpower_snmp_metadata(
    device: DeviceConfig,
    oid_map: dict[str, str] | None = None,
) -> _CyberPowerSnmpCache:
    """Refresh CyberPower SNMP metadata with dynamic OID resolution.

    Args:
        device: Device configuration
        oid_map: Optional mapping of canonical keys to OIDs; if None, uses default normalized fields
    """
    key = _cyberpower_cache_key(device)
    now = time.monotonic()
    with _CYBERPOWER_SNMP_CACHE_LOCK:
        cache = _CYBERPOWER_SNMP_CACHE.get(key)
        if cache is None:
            cache = _default_cyberpower_cache()
            _CYBERPOWER_SNMP_CACHE[key] = cache

        if (
            cache.last_refresh_monotonic > 0
            and (now - cache.last_refresh_monotonic) < METADATA_REFRESH_INTERVAL_SECONDS
        ):
            return cache

    # Use provided OID map or default to normalized fields
    if oid_map is None:
        oid_map = {
            "model": CYBERPOWER_OID_MODEL,
            "serial_number": CYBERPOWER_OID_SERIAL,
            "sw_version": CYBERPOWER_OID_FIRMWARE,
        }

    metadata = {
        "manufacturer": "CyberPower",
    }

    # Poll each OID dynamically
    for canonical_key, oid in oid_map.items():
        value = _snmp_get_sync(
            device.host, device.snmp_community, oid, port=device.snmp_port
        )
        if value:
            metadata[canonical_key] = value
            # Legacy compatibility aliases
            if canonical_key == "sw_version":
                metadata["firmware_version"] = value

    with _CYBERPOWER_SNMP_CACHE_LOCK:
        refreshed = _CyberPowerSnmpCache(
            metadata=metadata,
            last_refresh_monotonic=time.monotonic(),
        )
        _CYBERPOWER_SNMP_CACHE[key] = refreshed

    LOG.info(
        "[%s] CyberPower SNMP metadata (hourly): %s",
        device.id,
        ", ".join(f"{k}={bool(v)}" for k, v in metadata.items() if k != "manufacturer"),
    )

    return refreshed


def _maybe_refresh_ups_mib_snmp_metadata(device: DeviceConfig) -> _UpsMibSnmpCache:
    key = _ups_mib_cache_key(device)
    now = time.monotonic()
    with _UPS_MIB_SNMP_CACHE_LOCK:
        cache = _UPS_MIB_SNMP_CACHE.get(key)
        if cache is None:
            cache = _default_ups_mib_cache()
            _UPS_MIB_SNMP_CACHE[key] = cache

        if (
            cache.last_refresh_monotonic > 0
            and (now - cache.last_refresh_monotonic) < METADATA_REFRESH_INTERVAL_SECONDS
        ):
            return cache

    metadata: dict[str, str] = {}

    manufacturer = _snmp_get_sync(
        device.host,
        device.snmp_community,
        UPS_MIB_OID_MANUFACTURER,
        port=device.snmp_port,
    )
    if manufacturer:
        metadata["manufacturer"] = manufacturer

    model = _snmp_get_sync(
        device.host, device.snmp_community, UPS_MIB_OID_MODEL, port=device.snmp_port
    )
    if model:
        metadata["model"] = model

    firmware = _snmp_get_sync(
        device.host, device.snmp_community, UPS_MIB_OID_FIRMWARE, port=device.snmp_port
    )
    if firmware:
        metadata["firmware"] = firmware
        metadata["firmware_version"] = firmware
        metadata["sw_version"] = firmware

    name = _snmp_get_sync(
        device.host, device.snmp_community, UPS_MIB_OID_NAME, port=device.snmp_port
    )
    if name:
        metadata["name"] = name

    with _UPS_MIB_SNMP_CACHE_LOCK:
        refreshed = _UpsMibSnmpCache(
            metadata=metadata,
            last_refresh_monotonic=time.monotonic(),
        )
        _UPS_MIB_SNMP_CACHE[key] = refreshed

    LOG.info(
        "[%s] UPS-MIB SNMP metadata (hourly): manufacturer=%s, model=%s, sw_version=%s",
        device.id,
        bool(metadata.get("manufacturer")),
        bool(metadata.get("model")),
        bool(metadata.get("sw_version")),
    )

    return refreshed


def _merge_cyberpower_device_metadata(
    values: dict[str, Any], metadata: dict[str, str]
) -> None:
    """Merge CyberPower SNMP metadata into poll values with precedence rules.

    Precedence: SNMP metadata always wins for metadata fields (model, serial, firmware).
    These are authoritative from the device itself.
    """
    values.setdefault("manufacturer", "CyberPower")

    # Merge all metadata fields dynamically
    for key, value in metadata.items():
        if key == "manufacturer":
            continue  # Already set
        if value:
            values[key] = value
            # Legacy compatibility: sw_version also sets firmware_version
            if key == "sw_version":
                values.setdefault("firmware_version", value)


def _first_detected_probe_oid(
    device: DeviceConfig,
    oids: list[str],
    parser,
) -> str | None:
    for oid in oids:
        raw = _snmp_get_sync(
            device.host, device.snmp_community, oid, port=device.snmp_port
        )
        if parser(raw) is not None:
            return oid
    return None


def _merge_apc_device_metadata(
    values: dict[str, Any], metadata: dict[str, str]
) -> None:
    values.setdefault("manufacturer", "APC")
    model = metadata.get("model")
    if model:
        values["model"] = model
    serial = metadata.get("serial_number")
    if serial:
        values["serial_number"] = serial
    location = metadata.get("location")
    if location:
        values["location"] = location
    firmware = metadata.get("firmware_version")
    if firmware:
        values["firmware_version"] = firmware
        values.setdefault("firmware", firmware)
    fw_date = metadata.get("firmware_date")
    if fw_date:
        values["firmware_date"] = fw_date
        values.setdefault("hw_version", fw_date)


def _merge_apc_external_probe_data(
    device: DeviceConfig,
    values: dict[str, Any],
    detection: dict[str, str | None],
) -> None:
    if not detection:
        return

    effective = dict(detection)
    if values.get("input_frequency") is not None:
        effective["frequency_oid"] = None

    if not any(v for v in effective.values() if v):
        return

    mapping: list[tuple[str, str, Any]] = []
    freq_oid = effective.get("frequency_oid")
    if isinstance(freq_oid, str) and freq_oid:
        mapping.append(("input_frequency", freq_oid, _parse_frequency_hz))

    temp_1_oid = effective.get("temp_1_oid")
    if isinstance(temp_1_oid, str) and temp_1_oid:
        mapping.append(("measure_ups_temp_probe1", temp_1_oid, _parse_external_temp_c))
    hum_1_oid = effective.get("humidity_1_oid")
    if isinstance(hum_1_oid, str) and hum_1_oid:
        mapping.append(
            ("measure_ups_humidity_probe1", hum_1_oid, _parse_external_humidity_pct)
        )
    temp_2_oid = effective.get("temp_2_oid")
    if isinstance(temp_2_oid, str) and temp_2_oid:
        mapping.append(("measure_ups_temp_probe2", temp_2_oid, _parse_external_temp_c))
    hum_2_oid = effective.get("humidity_2_oid")
    if isinstance(hum_2_oid, str) and hum_2_oid:
        mapping.append(
            ("measure_ups_humidity_probe2", hum_2_oid, _parse_external_humidity_pct)
        )

    for key, oid, parser in mapping:
        raw = _snmp_get_sync(
            device.host, device.snmp_community, oid, port=device.snmp_port
        )
        parsed = parser(raw)
        if parsed is None:
            continue
        if key == "input_frequency" and values.get("input_frequency") is not None:
            continue
        values[key] = parsed


def _coerce_snmp_value(raw: str, spec: dict[str, Any]) -> int | float | str:
    parser_name = str(spec.get("parser", "")).strip().lower()
    if parser_name:
        if parser_name == "external_temp_c":
            parsed = _parse_external_temp_c(raw)
            if parsed is not None:
                return parsed
        elif parser_name == "external_humidity_pct":
            parsed = _parse_external_humidity_pct(raw)
            if parsed is not None:
                return parsed
        elif parser_name == "frequency_hz":
            parsed = _parse_frequency_hz(raw)
            if parsed is not None:
                return parsed

    text = raw.strip()
    try:
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            value: int | float = int(text)
        else:
            value = float(text)
            if value.is_integer():
                value = int(value)
    except ValueError:
        return text

    if spec.get("timeticks_minutes"):
        return int(float(value) / 6000)
    scale = spec.get("scale")
    if isinstance(scale, (int, float)) and scale not in (0, 1):
        return float(value) * float(scale)
    return value


def _poll_snmp_sync(
    device: DeviceConfig, profile: dict[str, Any], poll_groups: set[str] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    allowed_groups = poll_groups or {"slow"}

    # Runtime metadata is tracked independently from tier-gated sensor selection.
    # For APC-MIB SNMP drivers, always refresh identity cache so HA device info
    # stays populated even when metadata sensors are not enabled.
    if device.source == "ups_snmp_apc_mib":
        try:
            _maybe_refresh_apc_snmp_metadata(device)
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            LOG.debug("Runtime metadata refresh failed for %s: %s", device.source, err)
    elif device.source == "ups_snmp_ups_mib":
        try:
            _maybe_refresh_ups_mib_snmp_metadata(device)
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            LOG.debug("Runtime metadata refresh failed for %s: %s", device.source, err)

    all_oids = _filter_snmp_oids_by_catalog(device, profile)
    block_metrics: set[str] = set()
    has_block_for_group = False
    for block in profile.get("snmp_blocks", []):
        if not isinstance(block, dict):
            continue
        if str(block.get("poll_group", "slow")) not in allowed_groups:
            continue
        has_block_for_group = True
        metrics = block.get("metrics", [])
        if isinstance(metrics, list):
            block_metrics.update(str(item) for item in metrics)

    oids: dict[str, Any] = {}
    for key, spec in all_oids.items():
        if not isinstance(spec, dict):
            continue
        if str(spec.get("poll_group", "slow")) not in allowed_groups:
            continue
        if has_block_for_group and block_metrics and str(key) not in block_metrics:
            continue
        oids[str(key)] = spec

    for key, spec in oids.items():
        if not isinstance(spec, dict):
            continue
        candidates: list[str] = []
        if "oids" in spec:
            candidates = [str(item) for item in spec["oids"]]
        elif "oid" in spec:
            candidates = [str(spec["oid"])]
        for oid in candidates:
            raw = _snmp_get_sync(
                device.host, device.snmp_community, oid, port=device.snmp_port
            )
            if raw is None:
                continue
            out[str(key)] = _coerce_snmp_value(raw, spec)
            break
    return out


def _nut_guess_ups_name(device: DeviceConfig, profile: dict[str, Any]) -> str:
    configured = profile.get("nut", {}).get("ups_name")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    if device.name and device.name.strip():
        return device.name.strip()
    return device.id


def _nut_read_lines(sock: socket.socket, ups_name: str) -> list[str]:
    lines: list[str] = []
    with sock.makefile("rwb", buffering=0) as io:
        # Ignore greeting banner if present.
        try:
            _ = io.readline()
        except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            pass
        io.write(f"LIST VAR {ups_name}\n".encode("utf-8"))
        io.flush()
        while True:
            raw = io.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            lines.append(line)
            if line.startswith("END LIST VAR "):
                break
    return lines


def _nut_coerce(raw: str, declared_type: str) -> int | float | str | bool:
    dtype = declared_type.lower()
    if dtype == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if dtype == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    if dtype == "bool":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return raw


def _poll_nut_sync(
    device: DeviceConfig, profile: dict[str, Any], poll_groups: set[str] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    allowed_groups = poll_groups or {"slow"}
    nut = profile.get("nut", {})
    if not isinstance(nut, dict):
        return out

    variables = nut.get("variables", {})
    if not isinstance(variables, dict):
        return out

    ups_name = _nut_guess_ups_name(device, profile)
    port = int(device.port or 3493)

    sock = socket.create_connection((device.host, port), timeout=3.0)
    try:
        lines = _nut_read_lines(sock, ups_name)
    finally:
        sock.close()

    values_by_var: dict[str, str] = {}
    for line in lines:
        if not line.startswith("VAR "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) < 4:
            continue
        var_name = str(parts[2])
        var_value = str(parts[3])
        values_by_var[var_name] = var_value

    for var_name, spec in variables.items():
        if not isinstance(spec, dict):
            continue
        poll_group = str(spec.get("poll_group", "slow"))
        if poll_group not in allowed_groups:
            continue
        key = spec.get("key")
        if not isinstance(key, str) or not key:
            continue
        raw = values_by_var.get(str(var_name))
        if raw is None:
            continue
        out[key] = _nut_coerce(raw, str(spec.get("type", "str")))

    status_raw = values_by_var.get("ups.status", "")
    status_tokens = {
        token.strip() for token in status_raw.replace(",", " ").split() if token.strip()
    }
    status_map = nut.get("status_map", {})
    if isinstance(status_map, dict):
        for token, spec in status_map.items():
            if token not in status_tokens:
                continue
            if not isinstance(spec, dict):
                continue
            key = spec.get("key")
            if not isinstance(key, str) or not key:
                continue
            out[key] = spec.get("value", True)

    return out


def _merge_hybrid_values(
    device: DeviceConfig,
    profile: dict[str, Any],
    modbus_values: dict[str, Any],
    snmp_values: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(modbus_values)
    key_precedence = profile.get("key_precedence", {})
    if not isinstance(key_precedence, dict):
        key_precedence = {}
    for key, value in snmp_values.items():
        if key not in merged:
            merged[key] = value
            continue
        if key_precedence.get(key) == "snmp":
            merged[key] = value
            continue
        if key_precedence.get(key) == "modbus":
            continue
        collision_id = f"{device.source}:{key}"
        if collision_id not in _HYBRID_COLLISION_LOGGED:
            _HYBRID_COLLISION_LOGGED.add(collision_id)
            LOG.warning(
                "Hybrid key collision for %s metric=%s resolved with first-source fallback",
                device.source,
                key,
            )
    return merged


def _get_tier_config(device: DeviceConfig, profile: dict[str, Any]) -> dict[str, bool]:
    """Get tier configuration for field filtering.

    Precedence:
    1. Device-specific configuration (device.enable_extended_fields)
    2. Profile tier_model defaults

    Returns dict with enable_extended_fields boolean.
    """
    # Check device-specific enable_extended_fields attribute first
    if hasattr(device, "enable_extended_fields"):
        enabled = bool(device.enable_extended_fields)
        LOG.debug(
            "Tier config for %s from device attribute: extended=%s",
            device.source,
            enabled,
        )
        return {"enable_extended_fields": enabled}

    # Fall back to profile tier_model defaults
    tier_model = profile.get("tier_model", {})
    extended = tier_model.get("extended", {})
    enabled = bool(extended.get("enabled_by_default", False))

    LOG.debug(
        "Tier config for %s from profile default: extended=%s",
        device.source,
        enabled,
    )
    return {"enable_extended_fields": enabled}


def _merge_multi_source_with_validation(
    device: DeviceConfig,
    modbus_values: dict[str, Any],
    snmp_values: dict[str, Any],
) -> dict[str, Any]:
    """Merge multi-source results with explicit duplicate detection.

    CRITICAL: Each field must map to exactly one source.
    If any canonical key appears in both modbus_values and snmp_values,
    this is a schema violation and we must fail fast.
    """
    # Detect overlaps
    modbus_keys = set(modbus_values.keys())
    snmp_keys = set(snmp_values.keys())
    overlap = modbus_keys & snmp_keys

    if overlap:
        # FAIL FAST: Schema violation
        error_msg = (
            f"Multi-source schema violation for {device.source}: "
            f"Canonical keys appear in multiple transports: {sorted(overlap)}. "
            f"Each field must map to exactly one source. "
            f"Check catalog definition."
        )
        LOG.error(error_msg)
        raise ValueError(error_msg)

    # Safe to merge (no overlaps)
    merged = dict(modbus_values)
    merged.update(snmp_values)

    LOG.info(
        "Merged multi-source results for %s: %d modbus + %d snmp = %d total fields",
        device.source,
        len(modbus_values),
        len(snmp_values),
        len(merged),
    )

    return merged


async def _poll_multi_source(
    device: DeviceConfig,
    profile: dict[str, Any],
    groups: set[str],
) -> dict[str, Any]:
    """Poll multi-source driver with explicit tier/overlap validation.

    Planning Flow:
    1. Read catalog and tier config
    2. Filter fields by tier (before transport split)
    3. Read active_sources from profile
    4. Split filtered fields by transport
    5. Check if each transport has active fields
    6. Dispatch only transports with active fields
    7. Merge results with duplicate key detection
    """
    active_sources = profile.get("active_sources", {})
    modbus_config = active_sources.get("modbus", {})
    snmp_config = active_sources.get("snmp", {})

    # STEP 1: Tier filtering from DB catalog
    tier_config = _get_tier_config(device, profile)
    enable_extended = tier_config.get("enable_extended_fields", False)

    all_specs = [
        spec
        for spec in _catalog_sensor_specs(device)
        if str(spec.get("source", "")).strip().lower() in {"modbus", "snmp"}
    ]
    active_fields: list[dict[str, Any]] = []
    for spec in all_specs:
        tier = str(spec.get("tier", "normalized")).strip().lower() or "normalized"
        if tier == "extended" and not enable_extended:
            continue  # Skip extended fields if not enabled
        active_fields.append(spec)

    LOG.info(
        "Tier filtering for %s: %d total sensors, extended=%s, %d active after filter",
        device.source,
        len(all_specs),
        enable_extended,
        len(active_fields),
    )

    if not active_fields:
        # Fallback when catalog metadata is unavailable: dispatch based on configured mappings.
        modbus_fallback = len(
            [
                item
                for item in modbus_config.get("registers", [])
                if isinstance(item, dict) and str(item.get("key", "")).strip()
            ]
        )
        snmp_fallback = len(
            [
                item
                for item in dict(snmp_config.get("oids", {})).values()
                if isinstance(item, dict)
            ]
        )
        LOG.info(
            "No active catalog fields after tier filtering for %s; falling back to source mappings "
            "(modbus=%d, snmp=%d)",
            device.source,
            modbus_fallback,
            snmp_fallback,
        )
        active_fields = (
            [{"source": "modbus"}] * modbus_fallback
            + [{"source": "snmp"}] * snmp_fallback
        )

    # STEP 2: Split by transport
    modbus_fields: list[dict[str, Any]] = []
    snmp_fields: list[dict[str, Any]] = []
    for sensor in active_fields:
        source = str(sensor.get("source", "")).strip().lower()
        if source == "modbus":
            modbus_fields.append(sensor)
        elif source == "snmp":
            snmp_fields.append(sensor)

    LOG.debug(
        "Transport split for %s: %d modbus fields, %d snmp fields",
        device.source,
        len(modbus_fields),
        len(snmp_fields),
    )

    # STEP 3: Check active sources and field counts
    modbus_enabled = modbus_config.get("enabled", False)
    snmp_enabled = snmp_config.get("enabled", False)

    should_poll_modbus = modbus_enabled and len(modbus_fields) > 0
    should_poll_snmp = snmp_enabled and len(snmp_fields) > 0

    LOG.info(
        "Dispatch decision for %s: modbus=%s (%d fields), snmp=%s (%d fields)",
        device.source,
        "dispatched" if should_poll_modbus else "skipped",
        len(modbus_fields),
        "dispatched" if should_poll_snmp else "skipped",
        len(snmp_fields),
    )

    if not should_poll_modbus and not should_poll_snmp:
        LOG.info(
            "No transports dispatched for %s (modbus enabled=%s, snmp enabled=%s)",
            device.source,
            modbus_enabled,
            snmp_enabled,
        )
        return {}

    # STEP 4: Dispatch
    modbus_values = {}
    snmp_values = {}

    if should_poll_modbus:
        LOG.debug("Polling modbus for %s", device.source)
        modbus_values = await asyncio.to_thread(
            _poll_modbus_sync,
            device,
            modbus_config,
            groups,
            suppress_runtime_metadata_merge=True,
        )
        LOG.debug(
            "Modbus poll completed for %s: %d values", device.source, len(modbus_values)
        )

    if should_poll_snmp:
        LOG.debug("Polling snmp for %s", device.source)
        snmp_values = await asyncio.to_thread(
            _poll_snmp_sync, device, snmp_config, groups
        )
        LOG.debug(
            "SNMP poll completed for %s: %d values", device.source, len(snmp_values)
        )

    # STEP 5: Refresh runtime metadata cache (for discovery device info)
    # This populates metadata independently from sensor poll results
    if device.source.startswith("apc_modbus"):
        try:
            _maybe_refresh_apc_snmp_metadata(device)
            LOG.debug("Runtime metadata cache refreshed for %s", device.source)
        except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
            LOG.debug("Runtime metadata refresh failed for %s: %s", device.source, err)

    # STEP 6: Merge with duplicate detection
    return _merge_multi_source_with_validation(device, modbus_values, snmp_values)


async def poll_device(
    device: DeviceConfig,
    profile: dict[str, Any],
    poll_groups: set[str] | None = None,
) -> dict[str, Any]:
    groups = poll_groups or {"slow"}
    protocol = profile.get("protocol")
    if protocol == "modbus":
        return await asyncio.to_thread(_poll_modbus_sync, device, profile, groups)
    if protocol == "snmp":
        return await asyncio.to_thread(_poll_snmp_sync, device, profile, groups)
    if protocol == "hybrid":
        modbus_profile = profile.get("modbus", {})
        snmp_profile = profile.get("snmp", {})
        modbus_values = await asyncio.to_thread(
            _poll_modbus_sync, device, modbus_profile, groups
        )
        snmp_values = await asyncio.to_thread(
            _poll_snmp_sync, device, snmp_profile, groups
        )
        return _merge_hybrid_values(device, profile, modbus_values, snmp_values)
    if protocol == "multi_source":
        return await _poll_multi_source(device, profile, groups)
    if protocol == "nut":
        return await asyncio.to_thread(_poll_nut_sync, device, profile, groups)
    raise ValueError(f"Unsupported protocol for source {device.source}: {protocol}")
