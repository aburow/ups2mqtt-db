from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.main import _endpoint_lock_key
from ups2mqtt.model import DeviceConfig


def test_nut_endpoint_lock_key_differs_by_ups_name() -> None:
    profile = {"nut": {}}
    first = DeviceConfig(
        id="nut-1",
        source="nut_network_upsd",
        host="192.168.101.43",
        port=3493,
        ups_name="nutdev1",
    )
    second = DeviceConfig(
        id="nut-2",
        source="nut_network_upsd",
        host="192.168.101.43",
        port=3493,
        ups_name="apc_pdu1",
    )

    first_key = _endpoint_lock_key(
        runtime_source="nut_network_upsd",
        device=first,
        profile=profile,
    )
    second_key = _endpoint_lock_key(
        runtime_source="nut_network_upsd",
        device=second,
        profile=profile,
    )

    assert first_key == "nut_network_upsd://192.168.101.43:3493/nutdev1"
    assert second_key == "nut_network_upsd://192.168.101.43:3493/apc_pdu1"
    assert first_key != second_key


def test_nut_endpoint_lock_key_matches_when_ups_name_matches() -> None:
    profile = {"nut": {}}
    first = DeviceConfig(
        id="nut-1",
        source="nut_network_upsd",
        host="192.168.101.43",
        port=3493,
        ups_name="shared",
    )
    second = DeviceConfig(
        id="nut-2",
        source="nut_network_upsd",
        host="192.168.101.43",
        port=3493,
        ups_name="shared",
    )

    first_key = _endpoint_lock_key(
        runtime_source="nut_network_upsd",
        device=first,
        profile=profile,
    )
    second_key = _endpoint_lock_key(
        runtime_source="nut_network_upsd",
        device=second,
        profile=profile,
    )

    assert first_key == second_key


def test_apcupsd_endpoint_lock_key_remains_host_port_scoped() -> None:
    device = DeviceConfig(
        id="apcupsd-1",
        source="apcupsd_network_nis",
        host="192.168.101.36",
        port=3551,
        ups_name="ignored",
    )

    key = _endpoint_lock_key(
        runtime_source="apcupsd_network_nis",
        device=device,
        profile={"apcupsd": {}},
    )

    assert key == "apcupsd_network_nis://192.168.101.36:3551"


def test_modbus_endpoint_lock_key_remains_host_port_scoped() -> None:
    first = DeviceConfig(
        id="modbus-1",
        source="apc_modbus_smt",
        host="192.168.100.8",
        port=502,
        ups_name="first",
    )
    second = DeviceConfig(
        id="modbus-2",
        source="apc_modbus_smt",
        host="192.168.100.8",
        port=502,
        ups_name="second",
    )

    first_key = _endpoint_lock_key(
        runtime_source="apc_modbus_smt",
        device=first,
        profile={},
    )
    second_key = _endpoint_lock_key(
        runtime_source="apc_modbus_smt",
        device=second,
        profile={},
    )

    assert first_key == "apc_modbus_smt://192.168.100.8:502"
    assert second_key == first_key


def test_snmp_endpoint_lock_key_uses_snmp_port() -> None:
    device = DeviceConfig(
        id="snmp-1",
        source="ups_snmp_ups_mib",
        host="192.168.50.20",
        port=502,
        snmp_port=1161,
        snmp_community="private",
    )

    key = _endpoint_lock_key(
        runtime_source="ups_snmp_ups_mib",
        device=device,
        profile={"snmp": {}},
    )

    assert key == "ups_snmp_ups_mib://192.168.50.20:1161"
    assert "private" not in key
    assert ":502" not in key


def test_snmp_endpoint_lock_key_defaults_to_161_when_snmp_port_missing() -> None:
    device = DeviceConfig(
        id="snmp-2",
        source="ups_snmp_apc_mib",
        host="192.168.50.21",
        port=502,
        snmp_port=0,
    )

    key = _endpoint_lock_key(
        runtime_source="ups_snmp_apc_mib",
        device=device,
        profile={"snmp": {}},
    )

    assert key == "ups_snmp_apc_mib://192.168.50.21:161"
