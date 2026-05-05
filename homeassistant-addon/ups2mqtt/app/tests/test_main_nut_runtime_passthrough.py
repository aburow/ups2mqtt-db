from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.main import _resolve_runtime_profile
from ups2mqtt.model import DeviceConfig, ProfileConfig


def _nut_profile() -> dict[str, object]:
    return {
        "protocol": "nut",
        "profile_id": "nut_network_upsd",
        "source": "nut",
        "poll_groups": {
            "fast": {"interval_s": 15},
            "slow": {"interval_s": 60},
        },
        "nut": {
            "variables": {
                "battery.charge": {
                    "key": "battery_charge",
                    "poll_group": "fast",
                    "type": "float",
                }
            },
            "status_map": {},
        },
    }


def test_resolve_runtime_profile_keeps_selected_nut_raw_key_available() -> None:
    device = DeviceConfig(
        id="ups-1",
        source="nut_network_upsd",
        host="192.0.2.10",
        profile_uid="p1",
        profile_mode="global",
    )
    binding = ProfileConfig(
        profile_uid="p1",
        name="ION NUT Driver",
        driver_key="nut_network_upsd",
        config_payload={"driver_key": "nut_network_upsd"},
        selected_sensors=["battery_charge", "battery.voltage"],
        sensor_preferences={
            "battery_charge": {"mqtt_enabled": True},
            "battery.voltage": {"mqtt_enabled": True},
        },
    )
    runtime_source, effective_profile, discovery_keys, _signature = _resolve_runtime_profile(
        device=device,
        capability_profiles={"nut_network_upsd": _nut_profile()},
        profile_bindings={"p1": binding},
        apps_dir=None,
    )

    assert runtime_source == "nut_network_upsd"
    assert "battery_charge" in discovery_keys
    assert "battery.voltage" in discovery_keys

    nut = effective_profile.get("nut", {})
    variables = nut.get("variables", {}) if isinstance(nut, dict) else {}
    assert "battery.voltage" in variables
    assert variables["battery.voltage"]["key"] == "battery.voltage"


def test_resolve_runtime_profile_does_not_add_unselected_nut_raw_key() -> None:
    device = DeviceConfig(
        id="ups-1",
        source="nut_network_upsd",
        host="192.0.2.10",
        profile_uid="p1",
        profile_mode="global",
    )
    binding = ProfileConfig(
        profile_uid="p1",
        name="ION NUT Driver",
        driver_key="nut_network_upsd",
        config_payload={"driver_key": "nut_network_upsd"},
        selected_sensors=["battery_charge"],
        sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
    )
    _runtime_source, effective_profile, discovery_keys, _signature = _resolve_runtime_profile(
        device=device,
        capability_profiles={"nut_network_upsd": _nut_profile()},
        profile_bindings={"p1": binding},
        apps_dir=None,
    )

    assert "battery.voltage" not in discovery_keys
    nut = effective_profile.get("nut", {})
    variables = nut.get("variables", {}) if isinstance(nut, dict) else {}
    assert "battery.voltage" not in variables
