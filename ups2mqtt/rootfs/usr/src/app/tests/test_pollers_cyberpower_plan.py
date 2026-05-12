# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import logging
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
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "0")
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
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "0")
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
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "0")
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
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "0")
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
            (set(all_keys), set(all_keys)) if transport == "modbus" else (set(), set())
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
    assert all(
        _call_intersects_selected_pdu_descriptor(call, selected) for call in calls
    )
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


def test_optimizer_v2_dry_run_logs_debug_when_v2_matches_v1(
    monkeypatch, caplog
):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DRY_RUN", "1")
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", "balanced")
    monkeypatch.setattr(
        pollers,
        "_build_optimizer_v2_blocks",
        lambda _descriptors, _unsafe: [
            {"start_address": 0x2000, "count": 0x23},
            {"start_address": 0x3000, "count": 0x28},
            {"start_address": 0x3082, "count": 0x13},
        ],
    )
    monkeypatch.setattr(
        pollers,
        "_build_optimizer_v2_request_preserving_blocks",
        lambda _descriptors, _v1_blocks: [
            {"start_address": 0x2000, "count": 0x23},
            {"start_address": 0x3000, "count": 0x28},
            {"start_address": 0x3082, "count": 0x13},
        ],
    )
    with caplog.at_level(logging.DEBUG, logger="ups2mqtt.pollers"):
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
    comparison_records = [
        r for r in caplog.records if "Modbus optimizer_v2 comparison" in r.message
    ]
    assert comparison_records
    assert all(r.levelno == logging.DEBUG for r in comparison_records)
    log_text = "\n".join(r.message for r in comparison_records)
    assert "current_v1_plan" in log_text
    assert "proposed_v2_plan" in log_text
    assert "requests_saved" in log_text
    assert "registers_saved" in log_text
    assert "active_spans_covered" in log_text
    assert "orphan_blocks_removed" in log_text
    assert "forbidden_unsafe_ranges_avoided" in log_text
    assert "selected_candidate" in log_text
    assert "candidates" in log_text
    assert "policy" in log_text
    assert "dispatch_eligible" in log_text
    assert "ineligibility_reasons" in log_text


def test_optimizer_v2_dry_run_logs_info_when_v2_differs_from_v1(monkeypatch, caplog):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DRY_RUN", "1")
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", "min_bytes")
    monkeypatch.setattr(
        pollers,
        "_build_optimizer_v2_blocks",
        lambda _descriptors, _unsafe: [
            {"start_address": 0x2000, "count": 0x10},
        ],
    )
    with caplog.at_level(logging.DEBUG, logger="ups2mqtt.pollers"):
        _run_poll(
            monkeypatch,
            selected_keys={
                "battery_state_of_charge",
                "input_voltage",
                "on_battery_state",
                "output_load_percent",
                "runtime_remaining",
            },
        )
    comparison_records = [
        r for r in caplog.records if "Modbus optimizer_v2 comparison" in r.message
    ]
    assert comparison_records
    assert any(r.levelno == logging.INFO for r in comparison_records)


def test_optimizer_v2_disabled_emits_no_comparison_logs(monkeypatch, caplog):
    monkeypatch.delenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DRY_RUN", raising=False)
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "0")
    with caplog.at_level(logging.DEBUG, logger="ups2mqtt.pollers"):
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
    assert not any("Modbus optimizer_v2 comparison" in r.message for r in caplog.records)


def test_optimizer_v2_policy_default_is_balanced(monkeypatch):
    monkeypatch.delenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", raising=False)
    assert pollers._modbus_optimizer_v2_policy() == "balanced"  # noqa: SLF001
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", "invalid")
    assert pollers._modbus_optimizer_v2_policy() == "balanced"  # noqa: SLF001


def test_optimizer_v2_balanced_rejects_request_increase():
    eligible, reasons = pollers._optimizer_v2_dispatch_eligibility(  # noqa: SLF001
        policy="balanced",
        requests_saved=-1,
        registers_saved=10,
        active_spans_covered=True,
        unsafe_violation=False,
    )
    assert eligible is False
    assert any("request increase under balanced policy" in reason for reason in reasons)


def test_optimizer_v2_request_preserving_trim_keeps_request_count_and_saves_registers():
    descriptors = [
        {"key": "a", "address": 110, "count": 1},
        {"key": "b", "address": 111, "count": 1},
    ]
    v1_blocks = [{"name": "v1", "start_address": 100, "count": 20}]
    trimmed = pollers._build_optimizer_v2_request_preserving_blocks(  # noqa: SLF001
        descriptors,
        v1_blocks,
    )
    reqs = pollers._requests_from_blocks(trimmed)  # noqa: SLF001
    assert reqs == [(110, 2)]

    summary = pollers._optimizer_v2_candidate_summary(  # noqa: SLF001
        name="request_preserving",
        descriptors=descriptors,
        blocks=trimmed,
        unsafe_ranges=[],
        v1_requests=1,
        v1_registers=20,
    )
    assert summary["requests_saved"] == 0
    assert summary["registers_saved"] == 18
    eligible, reasons = pollers._optimizer_v2_dispatch_eligibility(  # noqa: SLF001
        policy="balanced",
        requests_saved=int(summary["requests_saved"]),
        registers_saved=int(summary["registers_saved"]),
        active_spans_covered=bool(summary["active_spans_covered"]),
        unsafe_violation=bool(summary["unsafe_violation"]),
    )
    assert eligible is True
    assert reasons == []


def test_optimizer_v2_balanced_rejects_when_only_min_bytes_saves_by_fragmenting(
    monkeypatch, caplog
):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DRY_RUN", "1")
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", "balanced")
    monkeypatch.setattr(
        pollers,
        "_build_optimizer_v2_blocks",
        lambda _descriptors, _unsafe: [
            {"start_address": 0x2000, "count": 0x01},
            {"start_address": 0x3000, "count": 0x01},
        ],
    )
    monkeypatch.setattr(
        pollers,
        "_build_optimizer_v2_request_preserving_blocks",
        lambda _descriptors, _v1_blocks: [
            {"start_address": 0x2000, "count": 0x23},
            {"start_address": 0x3000, "count": 0x28},
            {"start_address": 0x3082, "count": 0x13},
        ],
    )
    with caplog.at_level(logging.DEBUG, logger="ups2mqtt.pollers"):
        _run_poll(
            monkeypatch,
            selected_keys={
                "battery_state_of_charge",
                "input_voltage",
                "on_battery_state",
                "output_load_percent",
                "runtime_remaining",
            },
        )
    log_text = "\n".join(
        r.message for r in caplog.records if "Modbus optimizer_v2 comparison" in r.message
    )
    assert "dispatch_eligible': False" in log_text
    assert "no request-preserving candidate with positive register savings" in log_text


def test_optimizer_v2_min_requests_policy_rules():
    neg_eligible, neg_reasons = pollers._optimizer_v2_dispatch_eligibility(  # noqa: SLF001
        policy="min_requests",
        requests_saved=-1,
        registers_saved=10,
        active_spans_covered=True,
        unsafe_violation=False,
    )
    assert neg_eligible is False
    assert any("request increase under min_requests policy" in reason for reason in neg_reasons)

    zero_eligible, zero_reasons = pollers._optimizer_v2_dispatch_eligibility(  # noqa: SLF001
        policy="min_requests",
        requests_saved=0,
        registers_saved=0,
        active_spans_covered=True,
        unsafe_violation=False,
    )
    assert zero_eligible is True
    assert zero_reasons == []

    pos_eligible, pos_reasons = pollers._optimizer_v2_dispatch_eligibility(  # noqa: SLF001
        policy="min_requests",
        requests_saved=1,
        registers_saved=-5,
        active_spans_covered=True,
        unsafe_violation=False,
    )
    assert pos_eligible is True
    assert pos_reasons == []


def test_optimizer_v2_min_bytes_policy_allows_request_increase_with_byte_savings():
    eligible, reasons = pollers._optimizer_v2_dispatch_eligibility(  # noqa: SLF001
        policy="min_bytes",
        requests_saved=-2,
        registers_saved=15,
        active_spans_covered=True,
        unsafe_violation=False,
    )
    assert eligible is True
    assert reasons == []

    candidates = [
        {
            "name": "min_bytes",
            "plan": {"blocks": [(10, 1), (20, 1)], "singles": []},
            "requests_saved": -2,
            "registers_saved": 9,
            "active_spans_covered": True,
            "unsafe_violation": False,
        },
        {
            "name": "request_preserving",
            "plan": {"blocks": [(0, 12)], "singles": []},
            "requests_saved": 0,
            "registers_saved": 0,
            "active_spans_covered": True,
            "unsafe_violation": False,
        },
    ]
    selected = pollers._select_optimizer_v2_candidate(  # noqa: SLF001
        policy="min_bytes",
        candidates=candidates,
    )
    assert selected["name"] == "min_bytes"


def test_optimizer_v2_policy_safety_gates_override_all_policies():
    for policy in ("balanced", "min_requests", "min_bytes"):
        coverage_eligible, coverage_reasons = pollers._optimizer_v2_dispatch_eligibility(  # noqa: SLF001
            policy=policy,
            requests_saved=5,
            registers_saved=20,
            active_spans_covered=False,
            unsafe_violation=False,
        )
        assert coverage_eligible is False
        assert any("active spans not fully covered" in reason for reason in coverage_reasons)

        unsafe_eligible, unsafe_reasons = pollers._optimizer_v2_dispatch_eligibility(  # noqa: SLF001
            policy=policy,
            requests_saved=5,
            registers_saved=20,
            active_spans_covered=True,
            unsafe_violation=True,
        )
        assert unsafe_eligible is False
        assert any("forbidden/unsafe range violation" in reason for reason in unsafe_reasons)


def test_optimizer_v2_min_requests_prefers_fewer_requests_then_fewer_registers():
    candidates = [
        {
            "name": "a",
            "plan": {"blocks": [(0, 10)], "singles": []},
            "requests_saved": 1,
            "registers_saved": 1,
            "active_spans_covered": True,
            "unsafe_violation": False,
        },
        {
            "name": "b",
            "plan": {"blocks": [(0, 8)], "singles": []},
            "requests_saved": 1,
            "registers_saved": 4,
            "active_spans_covered": True,
            "unsafe_violation": False,
        },
        {
            "name": "c",
            "plan": {"blocks": [(0, 12)], "singles": []},
            "requests_saved": 0,
            "registers_saved": 9,
            "active_spans_covered": True,
            "unsafe_violation": False,
        },
    ]
    selected = pollers._select_optimizer_v2_candidate(  # noqa: SLF001
        policy="min_requests",
        candidates=candidates,
    )
    assert selected["name"] == "b"


def test_selected_transform_output_retains_required_modbus_source_descriptor(
    monkeypatch,
):
    class _FakeRepo:
        @staticmethod
        def load_catalog_derived_metrics(_driver_key):
            return [
                {
                    "output_key": "output_source_text",
                    "source_key": "output_source",
                    "transform": "enum_map",
                }
            ]

        @staticmethod
        def load_catalog_sensor_rows(_driver_key):
            return []

    monkeypatch.setattr(pollers, "get_capability_repository", lambda: _FakeRepo())
    descriptors = [
        {"key": "output_source", "address": 137, "count": 1},
        {"key": "battery_status", "address": 138, "count": 1},
    ]
    filtered = pollers._filter_modbus_descriptors_by_selected_keys(  # noqa: SLF001
        _smt_device(),
        descriptors,
        {"output_source_text"},
    )
    keys = {str(item.get("key")) for item in filtered}
    assert "output_source" in keys
    assert "battery_status" not in keys


def _v2_payload(
    *,
    selected_candidate: str,
    blocks: list[tuple[int, int]],
    dispatch_eligible: bool,
    ineligibility_reasons: list[str] | None = None,
) -> dict[str, object]:
    baseline_registers = sum(count for _start, count in _BASELINE_CP_BLOCKS)
    candidate_registers = sum(count for _start, count in blocks)
    return {
        "selected_candidate": selected_candidate,
        "dispatch_eligible": dispatch_eligible,
        "ineligibility_reasons": list(ineligibility_reasons or []),
        "proposed_v2_plan": {"blocks": list(blocks), "singles": []},
        "candidates": [
            {
                "name": selected_candidate,
                "plan": {"blocks": list(blocks), "singles": []},
                "requests_saved": len(_BASELINE_CP_BLOCKS) - len(blocks),
                "registers_saved": baseline_registers - candidate_registers,
                "active_spans_covered": dispatch_eligible,
                "unsafe_violation": False,
            }
        ],
    }


_BASELINE_CP_BLOCKS = [(0x3000, 0x28), (0x3082, 0x13)]


def _run_poll_capture_dispatched_blocks(
    monkeypatch,
    *,
    payload: dict[str, object],
    device: DeviceConfig | None = None,
) -> list[list[dict[str, object]]]:
    captured_blocks: list[list[dict[str, object]]] = []
    session = SimpleNamespace(
        lock=threading.Lock(),
        client=_FakeModbusClient(),
        resolved_unit_param=None,
        reconnect_count=0,
        recreate_count=0,
        last_io_monotonic=0.0,
    )
    monkeypatch.setattr(pollers, "_get_modbus_session", lambda _endpoint: session)
    monkeypatch.setattr(pollers, "_ensure_connected", lambda _session, _device: True)
    monkeypatch.setattr(
        pollers,
        "_catalog_alias_to_canonical_map",
        lambda _device, transport: {},
    )
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
        "_build_optimizer_v2_comparison_payload",
        lambda *_args, **_kwargs: (payload, True),
    )

    def _capture_try_block_reads(
        _session, _device, _descriptors, register_blocks, _output, _decoded
    ):
        captured_blocks.append([dict(item) for item in register_blocks])
        return False

    monkeypatch.setattr(pollers, "_try_block_reads", _capture_try_block_reads)
    monkeypatch.setattr(
        pollers,
        "_try_individual_reads",
        lambda *_args, **_kwargs: None,
    )
    pollers._poll_modbus_sync(  # noqa: SLF001
        device or _device(),
        get_single_phase_profile(),
        {"fast"},
        suppress_runtime_metadata_merge=True,
        selected_keys={
            "battery_state_of_charge",
            "input_voltage",
            "on_battery_state",
            "output_load_percent",
            "runtime_remaining",
        },
    )
    return captured_blocks


def test_optimizer_v2_dispatch_default_enabled_uses_v2(monkeypatch):
    monkeypatch.delenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", raising=False)
    monkeypatch.delenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISABLED_DEVICES", raising=False)
    payload = _v2_payload(
        selected_candidate="request_preserving",
        blocks=[(0x2001, 0x10)],
        dispatch_eligible=True,
    )
    captured = _run_poll_capture_dispatched_blocks(monkeypatch, payload=payload)
    assert captured
    first = captured[0]
    assert [(int(item["start_address"]), int(item["count"])) for item in first] == [
        (0x2001, 0x10),
    ]


def test_optimizer_v2_dispatch_explicit_false_uses_v1(monkeypatch):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "0")
    payload = _v2_payload(
        selected_candidate="request_preserving",
        blocks=[(0x2001, 0x10)],
        dispatch_eligible=True,
    )
    captured = _run_poll_capture_dispatched_blocks(monkeypatch, payload=payload)
    assert [(int(item["start_address"]), int(item["count"])) for item in captured[0]] == [*_BASELINE_CP_BLOCKS]


def test_optimizer_v2_dispatch_device_setting_disabled_uses_v1(monkeypatch, caplog):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "1")
    payload = _v2_payload(
        selected_candidate="request_preserving",
        blocks=[(0x3000, 0x10)],
        dispatch_eligible=True,
    )
    disabled_device = _device()
    disabled_device.optimizer_v2_enabled = False
    with caplog.at_level(logging.INFO, logger="ups2mqtt.pollers"):
        captured = _run_poll_capture_dispatched_blocks(
            monkeypatch, payload=payload, device=disabled_device
        )
    assert "device setting" in "\n".join(r.message for r in caplog.records)
    assert [(int(item["start_address"]), int(item["count"])) for item in captured[0]] == [*_BASELINE_CP_BLOCKS]


def test_optimizer_v2_dispatch_eligible_uses_v2(monkeypatch):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "1")
    monkeypatch.delenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISABLED_DEVICES", raising=False)
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", "balanced")
    payload = _v2_payload(
        selected_candidate="request_preserving",
        blocks=[(0x3000, 0x28), (0x3082, 0x03)],
        dispatch_eligible=True,
    )
    captured = _run_poll_capture_dispatched_blocks(monkeypatch, payload=payload)
    assert [(int(item["start_address"]), int(item["count"])) for item in captured[0]] == [
        (0x3000, 0x28),
        (0x3082, 0x03),
    ]


def test_optimizer_v2_dispatch_ineligible_uses_v1(monkeypatch, caplog):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "1")
    monkeypatch.delenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISABLED_DEVICES", raising=False)
    payload = _v2_payload(
        selected_candidate="request_preserving",
        blocks=[(0x3000, 0x22)],
        dispatch_eligible=False,
        ineligibility_reasons=["request increase under balanced policy"],
    )
    with caplog.at_level(logging.INFO, logger="ups2mqtt.pollers"):
        captured = _run_poll_capture_dispatched_blocks(monkeypatch, payload=payload)
    assert "optimizer_v2 dispatch rejected" in "\n".join(r.message for r in caplog.records)
    assert [(int(item["start_address"]), int(item["count"])) for item in captured[0]] == [*_BASELINE_CP_BLOCKS]


def test_optimizer_v2_dispatch_rejects_request_increase_in_balanced(monkeypatch):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "1")
    monkeypatch.delenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISABLED_DEVICES", raising=False)
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", "balanced")
    payload = _v2_payload(
        selected_candidate="request_preserving",
        blocks=[(0x3000, 0x01), (0x3010, 0x01), (0x3082, 0x01)],
        dispatch_eligible=True,
    )
    captured = _run_poll_capture_dispatched_blocks(monkeypatch, payload=payload)
    assert [(int(item["start_address"]), int(item["count"])) for item in captured[0]] == [*_BASELINE_CP_BLOCKS]


def test_optimizer_v2_dispatch_never_uses_min_bytes_fragmented_candidate(monkeypatch):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "1")
    monkeypatch.delenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISABLED_DEVICES", raising=False)
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", "balanced")
    payload = _v2_payload(
        selected_candidate="min_bytes",
        blocks=[(0x3000, 0x28), (0x3082, 0x03)],
        dispatch_eligible=True,
    )
    captured = _run_poll_capture_dispatched_blocks(monkeypatch, payload=payload)
    assert [(int(item["start_address"]), int(item["count"])) for item in captured[0]] == [*_BASELINE_CP_BLOCKS]

    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", "min_bytes")
    captured = _run_poll_capture_dispatched_blocks(monkeypatch, payload=payload)
    assert [(int(item["start_address"]), int(item["count"])) for item in captured[0]] == [*_BASELINE_CP_BLOCKS]


def test_optimizer_v2_dispatch_uses_safe_candidate_when_policy_selected_min_bytes(monkeypatch):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "1")
    monkeypatch.delenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISABLED_DEVICES", raising=False)
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", "min_bytes")
    payload = {
        "selected_candidate": "min_bytes",
        "dispatch_eligible": True,
        "ineligibility_reasons": [],
        "proposed_v2_plan": {
            "blocks": [(0x3000, 0x01), (0x3010, 0x01), (0x3082, 0x01)],
            "singles": [],
        },
        "candidates": [
            {
                "name": "min_bytes",
                "plan": {
                    "blocks": [(0x3000, 0x01), (0x3010, 0x01), (0x3082, 0x01)],
                    "singles": [],
                },
                "requests_saved": -1,
                "registers_saved": 56,
                "active_spans_covered": True,
                "unsafe_violation": False,
            },
            {
                "name": "request_preserving",
                "plan": {"blocks": [(0x3000, 0x28), (0x3082, 0x03)], "singles": []},
                "requests_saved": 0,
                "registers_saved": 16,
                "active_spans_covered": True,
                "unsafe_violation": False,
            },
        ],
    }
    captured = _run_poll_capture_dispatched_blocks(monkeypatch, payload=payload)
    assert [(int(item["start_address"]), int(item["count"])) for item in captured[0]] == [
        (0x3000, 0x28),
        (0x3082, 0x03),
    ]


def test_optimizer_v2_dispatch_failure_falls_back_and_latches_v1(monkeypatch):
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISPATCH", "1")
    monkeypatch.delenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_DISABLED_DEVICES", raising=False)
    monkeypatch.setenv("UPS2MQTT_MODBUS_OPTIMIZER_V2_POLICY", "balanced")
    payload = _v2_payload(
        selected_candidate="request_preserving",
        blocks=[(0x3000, 0x28), (0x3082, 0x03)],
        dispatch_eligible=True,
    )
    monkeypatch.setattr(
        pollers,
        "_build_optimizer_v2_comparison_payload",
        lambda *_args, **_kwargs: (payload, True),
    )
    session = SimpleNamespace(
        lock=threading.Lock(),
        client=_FakeModbusClient(),
        resolved_unit_param=None,
        reconnect_count=0,
        recreate_count=0,
        last_io_monotonic=0.0,
    )
    monkeypatch.setattr(pollers, "_get_modbus_session", lambda _endpoint: session)
    monkeypatch.setattr(pollers, "_ensure_connected", lambda _session, _device: True)
    monkeypatch.setattr(
        pollers,
        "_catalog_alias_to_canonical_map",
        lambda _device, transport: {},
    )
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
        "_try_individual_reads",
        lambda *_args, **_kwargs: None,
    )
    with pollers._MODBUS_V2_FALLBACK_DEVICES_LOCK:  # noqa: SLF001
        pollers._MODBUS_V2_FALLBACK_DEVICES.clear()  # noqa: SLF001

    captured_blocks: list[list[dict[str, object]]] = []
    call_count = {"n": 0}

    def _fail_first_block_reads(
        _session, _device, _descriptors, register_blocks, _output, _decoded
    ):
        captured_blocks.append([dict(item) for item in register_blocks])
        call_count["n"] += 1
        return call_count["n"] == 1

    monkeypatch.setattr(pollers, "_try_block_reads", _fail_first_block_reads)
    pollers._poll_modbus_sync(  # noqa: SLF001
        _device(),
        get_single_phase_profile(),
        {"fast"},
        suppress_runtime_metadata_merge=True,
        selected_keys={
            "battery_state_of_charge",
            "input_voltage",
            "on_battery_state",
            "output_load_percent",
            "runtime_remaining",
        },
    )
    assert len(captured_blocks) == 2
    assert [(int(item["start_address"]), int(item["count"])) for item in captured_blocks[0]] == [
        (0x3000, 0x28),
        (0x3082, 0x03),
    ]
    assert [(int(item["start_address"]), int(item["count"])) for item in captured_blocks[1]] == [*_BASELINE_CP_BLOCKS]

    captured_blocks.clear()
    pollers._poll_modbus_sync(  # noqa: SLF001
        _device(),
        get_single_phase_profile(),
        {"fast"},
        suppress_runtime_metadata_merge=True,
        selected_keys={
            "battery_state_of_charge",
            "input_voltage",
            "on_battery_state",
            "output_load_percent",
            "runtime_remaining",
        },
    )
    assert len(captured_blocks) == 1
    assert [(int(item["start_address"]), int(item["count"])) for item in captured_blocks[0]] == [*_BASELINE_CP_BLOCKS]


def test_modbus_keepalive_session_key_includes_device_identity() -> None:
    device_a = DeviceConfig(
        id="dev-a",
        device_uid="uid-a",
        source="cyberpower_modbus_single_phase",
        host="192.0.2.10",
        port=502,
        unit_id=1,
        keep_connection_open=True,
    )
    device_b = DeviceConfig(
        id="dev-b",
        device_uid="uid-b",
        source="cyberpower_modbus_single_phase",
        host="192.0.2.10",
        port=502,
        unit_id=1,
        keep_connection_open=True,
    )
    key_a = pollers._modbus_session_key(device_a)  # noqa: SLF001
    key_b = pollers._modbus_session_key(device_b)  # noqa: SLF001
    assert key_a != key_b


def test_modbus_keepalive_session_key_distinguishes_unit_id() -> None:
    device_a = DeviceConfig(
        id="dev-a",
        source="cyberpower_modbus_single_phase",
        host="192.0.2.10",
        port=502,
        unit_id=1,
        keep_connection_open=True,
    )
    device_b = DeviceConfig(
        id="dev-a",
        source="cyberpower_modbus_single_phase",
        host="192.0.2.10",
        port=502,
        unit_id=2,
        keep_connection_open=True,
    )
    key_a = pollers._modbus_session_key(device_a)  # noqa: SLF001
    key_b = pollers._modbus_session_key(device_b)  # noqa: SLF001
    assert key_a != key_b


def test_close_modbus_keepalive_for_device_does_not_close_other_device_session() -> None:
    class _ClosableClient:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    device_a = DeviceConfig(
        id="dev-a",
        device_uid="uid-a",
        source="cyberpower_modbus_single_phase",
        host="192.0.2.10",
        port=502,
        unit_id=1,
        keep_connection_open=True,
    )
    device_b = DeviceConfig(
        id="dev-b",
        device_uid="uid-b",
        source="cyberpower_modbus_single_phase",
        host="192.0.2.10",
        port=502,
        unit_id=1,
        keep_connection_open=True,
    )

    client_a = _ClosableClient()
    client_b = _ClosableClient()
    session_a = pollers._EndpointSession()  # noqa: SLF001
    session_b = pollers._EndpointSession()  # noqa: SLF001
    session_a.client = client_a
    session_b.client = client_b
    key_a = pollers._modbus_session_key(device_a)  # noqa: SLF001
    key_b = pollers._modbus_session_key(device_b)  # noqa: SLF001

    with pollers._MODBUS_SESSIONS_LOCK:  # noqa: SLF001
        pollers._MODBUS_SESSIONS.clear()  # noqa: SLF001
        pollers._MODBUS_SESSIONS[key_a] = session_a  # noqa: SLF001
        pollers._MODBUS_SESSIONS[key_b] = session_b  # noqa: SLF001

    pollers.close_modbus_keepalive_for_device(device_a)
    assert client_a.close_calls == 1
    assert client_b.close_calls == 0

    with pollers._MODBUS_SESSIONS_LOCK:  # noqa: SLF001
        assert key_a not in pollers._MODBUS_SESSIONS  # noqa: SLF001
        assert key_b in pollers._MODBUS_SESSIONS  # noqa: SLF001

    pollers.close_modbus_keepalive_for_device(device_b)
    assert client_b.close_calls == 1
