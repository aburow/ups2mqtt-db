# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from ups2mqtt.drivers.apc_modbus.profiles import get_smt_profile
from ups2mqtt.drivers.apc_modbus.profiles import get_smart_profile
from ups2mqtt.drivers.apc_modbus.profiles import get_rack_pdu_profile
from ups2mqtt.drivers.cyberpower_modbus.profiles import get_single_phase_profile
from ups2mqtt.model import DeviceConfig
from ups2mqtt import pollers


class _FakeReadResult:
    def __init__(self, count: int) -> None:
        self.registers = [0] * count

    def isError(self) -> bool:
        return False


class _FakeModbusClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int | None]] = []

    def read_holding_registers(
        self, address: int, count: int = 1, unit: int | None = None
    ) -> _FakeReadResult:
        self.calls.append((int(address), int(count), unit))
        return _FakeReadResult(int(count))


def _device() -> DeviceConfig:
    return DeviceConfig(
        id="cp-min",
        source="cyberpower_modbus_single_phase",
        host="127.0.0.1",
        port=502,
        unit_id=1,
        keep_connection_open=True,
    )


def _smt_device() -> DeviceConfig:
    return DeviceConfig(
        id="apc-smt-min",
        source="apc_modbus_smt",
        host="127.0.0.1",
        port=502,
        unit_id=1,
        keep_connection_open=True,
    )


def _smart_device() -> DeviceConfig:
    return DeviceConfig(
        id="apc-smart-min",
        source="apc_modbus_smart",
        host="127.0.0.1",
        port=502,
        unit_id=1,
        keep_connection_open=True,
    )


def _pdu_device() -> DeviceConfig:
    return DeviceConfig(
        id="apc-pdu-min",
        source="apc_modbus_rack_pdu",
        host="127.0.0.1",
        port=502,
        unit_id=1,
        keep_connection_open=True,
    )


def _run_poll(
    monkeypatch,
    *,
    selected_keys: set[str],
    poll_groups: set[str] | None = None,
) -> list[tuple[int, int, int | None]]:
    client = _FakeModbusClient()
    session = SimpleNamespace(
        lock=threading.Lock(),
        client=client,
        resolved_unit_param=None,
        reconnect_count=0,
        recreate_count=0,
        last_io_monotonic=0.0,
    )

    monkeypatch.setattr(pollers, "_get_modbus_session", lambda _endpoint: session)
    monkeypatch.setattr(pollers, "_ensure_connected", lambda _session, _device: True)
    monkeypatch.setattr(
        pollers,
        "_catalog_keys_for_transport",
        lambda _device, transport: (
            {
                "hardware_fault",
                "utility_frequency_out_of_range",
                "inverter_off",
                "battery_not_present",
                "battery_discharging",
                "battery_charging",
                "battery_fully_charged",
                "buzzer_muted",
                "runtime_low",
                "no_output",
                "over_temperature",
                "utility_voltage",
                "utility_frequency",
                "output_voltage",
                "output_load_percent",
                "battery_capacity",
                "runtime_remaining",
                "battery_threshold",
                "runtime_threshold",
            },
            {
                "hardware_fault",
                "utility_frequency_out_of_range",
                "inverter_off",
                "battery_not_present",
                "battery_discharging",
                "battery_charging",
                "battery_fully_charged",
                "buzzer_muted",
                "runtime_low",
                "no_output",
                "over_temperature",
                "utility_voltage",
                "utility_frequency",
                "output_voltage",
                "output_load_percent",
                "battery_capacity",
                "runtime_remaining",
                "battery_threshold",
                "runtime_threshold",
            },
        ),
    )
    monkeypatch.setattr(
        pollers,
        "_catalog_alias_to_canonical_map",
        lambda _device, transport: {
            "battery_discharging": "on_battery_state",
            "battery_capacity": "battery_state_of_charge",
            "utility_voltage": "input_voltage",
        },
    )

    pollers._poll_modbus_sync(  # noqa: SLF001
        _device(),
        get_single_phase_profile(),
        poll_groups or {"fast"},
        suppress_runtime_metadata_merge=True,
        selected_keys=selected_keys,
    )
    return client.calls


def _run_smt_poll(
    monkeypatch,
    *,
    selected_keys: set[str],
) -> list[tuple[int, int, int | None]]:
    profile = get_smt_profile()
    modbus_profile = profile["active_sources"]["modbus"]
    registers = modbus_profile["registers"]
    for item in registers:
        key = str(item.get("key", ""))
        if key == "battery_state_of_charge":
            item["address"] = 0
        elif key == "input_voltage":
            item["address"] = 2
        elif key == "output_load_percent":
            item["address"] = 3
        elif key == "runtime_remaining":
            item["address"] = 4

    client = _FakeModbusClient()
    session = SimpleNamespace(
        lock=threading.Lock(),
        client=client,
        resolved_unit_param=None,
        reconnect_count=0,
        recreate_count=0,
        last_io_monotonic=0.0,
    )
    monkeypatch.setattr(pollers, "_get_modbus_session", lambda _endpoint: session)
    monkeypatch.setattr(pollers, "_ensure_connected", lambda _session, _device: True)
    monkeypatch.setattr(
        pollers,
        "_catalog_sensor_specs",
        lambda _device: [
            {"key": "battery_state_of_charge", "source": "modbus"},
            {"key": "input_voltage", "source": "modbus"},
            {"key": "output_load_percent", "source": "modbus"},
            {"key": "runtime_remaining", "source": "modbus"},
            {"key": "battery_temperature", "source": "modbus"},
            {"key": "output_source", "source": "snmp"},
        ],
    )
    monkeypatch.setattr(
        pollers,
        "_catalog_keys_for_transport",
        lambda _device, transport: (
            (
                {
                    "battery_state_of_charge",
                    "input_voltage",
                    "output_load_percent",
                    "runtime_remaining",
                    "battery_temperature",
                }
                if transport == "modbus"
                else {"output_source"}
            ),
            (
                {
                    "battery_state_of_charge",
                    "input_voltage",
                    "output_load_percent",
                    "runtime_remaining",
                    "battery_temperature",
                }
                if transport == "modbus"
                else {"output_source"}
            ),
        ),
    )
    monkeypatch.setattr(
        pollers,
        "_catalog_alias_to_canonical_map",
        lambda _device, transport: {},
    )
    monkeypatch.setattr(
        pollers,
        "_poll_snmp_sync",
        lambda _device, _profile, _groups: {},
    )

    asyncio.run(
        pollers.poll_device(
            _smt_device(),
            profile,
            {"fast", "slow"},
            selected_keys=selected_keys,
        )
    )
    return client.calls


def _run_smart_poll(
    monkeypatch,
    *,
    selected_keys: set[str],
) -> list[tuple[int, int, int | None]]:
    profile = get_smart_profile()
    client = _FakeModbusClient()
    session = SimpleNamespace(
        lock=threading.Lock(),
        client=client,
        resolved_unit_param=None,
        reconnect_count=0,
        recreate_count=0,
        last_io_monotonic=0.0,
    )
    monkeypatch.setattr(pollers, "_get_modbus_session", lambda _endpoint: session)
    monkeypatch.setattr(pollers, "_ensure_connected", lambda _session, _device: True)
    monkeypatch.setattr(
        pollers,
        "_catalog_sensor_specs",
        lambda _device: [
            {"key": "battery_state_of_charge", "source": "modbus"},
            {"key": "input_voltage", "source": "modbus"},
            {"key": "load_percent", "source": "modbus"},
            {"key": "runtime_remaining", "source": "modbus"},
            {"key": "lower_transfer_point", "source": "modbus"},
            {"key": "output_source", "source": "snmp"},
        ],
    )
    monkeypatch.setattr(
        pollers,
        "_catalog_keys_for_transport",
        lambda _device, transport: (
            (
                {
                    "battery_state_of_charge",
                    "input_voltage",
                    "load_percent",
                    "runtime_remaining",
                    "lower_transfer_point",
                }
                if transport == "modbus"
                else {"output_source"}
            ),
            (
                {
                    "battery_state_of_charge",
                    "input_voltage",
                    "load_percent",
                    "runtime_remaining",
                    "lower_transfer_point",
                }
                if transport == "modbus"
                else {"output_source"}
            ),
        ),
    )
    monkeypatch.setattr(
        pollers,
        "_catalog_alias_to_canonical_map",
        lambda _device, transport: (
            {"output_load_percent": "load_percent"} if transport == "modbus" else {}
        ),
    )
    monkeypatch.setattr(
        pollers,
        "_poll_snmp_sync",
        lambda _device, _profile, _groups: {},
    )

    asyncio.run(
        pollers.poll_device(
            _smart_device(),
            profile,
            {"fast", "slow"},
            selected_keys=selected_keys,
        )
    )
    return client.calls


def _run_pdu_poll(
    monkeypatch,
    *,
    selected_keys: set[str],
) -> list[tuple[int, int, int | None]]:
    profile = get_rack_pdu_profile()
    client = _FakeModbusClient()
    session = SimpleNamespace(
        lock=threading.Lock(),
        client=client,
        resolved_unit_param=None,
        reconnect_count=0,
        recreate_count=0,
        last_io_monotonic=0.0,
    )
    monkeypatch.setattr(pollers, "_get_modbus_session", lambda _endpoint: session)
    monkeypatch.setattr(pollers, "_ensure_connected", lambda _session, _device: True)
    all_keys = {str(item["key"]) for item in profile["registers"]}
    monkeypatch.setattr(
        pollers,
        "_catalog_keys_for_transport",
        lambda _device, transport: (
            (set(all_keys), set(all_keys))
            if transport == "modbus"
            else (set(), set())
        ),
    )
    monkeypatch.setattr(
        pollers,
        "_catalog_alias_to_canonical_map",
        lambda _device, transport: {},
    )

    pollers._poll_modbus_sync(  # noqa: SLF001
        _pdu_device(),
        profile,
        {"fast", "slow"},
        suppress_runtime_metadata_merge=True,
        selected_keys=selected_keys,
    )
    return client.calls


def _call_intersects_selected_pdu_descriptor(
    call: tuple[int, int, int | None],
    selected_keys: set[str],
) -> bool:
    start, count, _unit = call
    end = start + count
    for item in get_rack_pdu_profile()["registers"]:
        key = str(item["key"])
        if key not in selected_keys:
            continue
        address = int(item["address"])
        reg_count = int(item.get("count", 1))
        desc_end = address + reg_count
        if address < end and desc_end > start:
            return True
    return False


def test_cyberpower_minimal_profile_dispatches_only_expected_blocks(monkeypatch):
    calls = _run_poll(
        monkeypatch,
        selected_keys={
            "battery_state_of_charge",
            "input_voltage",
            "on_battery_state",
            "output_load_percent",
            "runtime_remaining",
        },
    )
    assert calls == [
        (0x2000, 0x23, 1),
        (0x3000, 0x28, 1),
        (0x3082, 0x13, 1),
    ]


def test_cyberpower_over_temperature_enabled_includes_229c(monkeypatch):
    calls = _run_poll(
        monkeypatch,
        selected_keys={
            "battery_state_of_charge",
            "input_voltage",
            "on_battery_state",
            "output_load_percent",
            "runtime_remaining",
            "over_temperature",
        },
        poll_groups={"fast", "slow"},
    )
    assert (0x229C, 1, 1) in calls


def test_cyberpower_over_temperature_disabled_excludes_229c(monkeypatch):
    calls = _run_poll(
        monkeypatch,
        selected_keys={
            "battery_state_of_charge",
            "input_voltage",
            "on_battery_state",
            "output_load_percent",
            "runtime_remaining",
        },
    )
    assert (0x229C, 1, 1) not in calls


def test_apc_smt_minimal_profile_excludes_upper_measurements_block(monkeypatch):
    calls = _run_smt_poll(
        monkeypatch,
        selected_keys={
            "battery_state_of_charge",
            "input_voltage",
            "output_load_percent",
            "runtime_remaining",
        },
    )
    assert (0x0000, 0x0017, 1) in calls
    assert (0x0080, 0x001A, 1) not in calls
    assert not any(
        start >= 0x0080 and (start + count) <= 0x009A for start, count, _unit in calls
    )


def test_apc_smt_upper_block_sensor_enabled_includes_upper_block(monkeypatch):
    calls = _run_smt_poll(
        monkeypatch,
        selected_keys={
            "battery_state_of_charge",
            "input_voltage",
            "output_load_percent",
            "runtime_remaining",
            "battery_temperature",
        },
    )
    assert (0x0080, 0x001A, 1) in calls


def test_apc_smart_minimal_profile_excludes_001b_block(monkeypatch):
    calls = _run_smart_poll(
        monkeypatch,
        selected_keys={
            "battery_state_of_charge",
            "input_voltage",
            "output_load_percent",
            "runtime_remaining",
        },
    )
    assert (0x0000, 0x0013, 1) in calls
    assert (0x001B, 0x0011, 1) not in calls
    assert not any(
        start >= 0x001B and (start + count) <= 0x002C for start, count, _unit in calls
    )


def test_apc_smart_upper_block_sensor_enabled_includes_001b_block(monkeypatch):
    calls = _run_smart_poll(
        monkeypatch,
        selected_keys={
            "battery_state_of_charge",
            "input_voltage",
            "output_load_percent",
            "runtime_remaining",
            "lower_transfer_point",
        },
    )
    assert (0x001B, 0x0011, 1) in calls


def test_apc_pdu_plan_only_contains_selected_descriptor_intersections(monkeypatch):
    selected = {"device_real_power", "phase_L1_current"}
    calls = _run_pdu_poll(monkeypatch, selected_keys=selected)
    assert calls
    assert all(_call_intersects_selected_pdu_descriptor(call, selected) for call in calls)
    assert (0x009E, 0x0005, 1) not in calls


def test_apc_pdu_optional_capability_block_only_when_selected(monkeypatch):
    baseline_calls = _run_pdu_poll(
        monkeypatch,
        selected_keys={"device_real_power", "phase_L1_current"},
    )
    assert (0x009E, 0x0005, 1) not in baseline_calls

    with_optional_calls = _run_pdu_poll(
        monkeypatch,
        selected_keys={"device_real_power", "phase_L1_current", "num_outlets"},
    )
    assert (0x009E, 0x0005, 1) in with_optional_calls
