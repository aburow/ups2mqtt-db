from __future__ import annotations

import logging
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.capability_repository import configure_capability_repository
from ups2mqtt.database import Database
import ups2mqtt.transforms as transforms_module


@pytest.fixture(autouse=True)
def _seed_capability_repo(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "capabilities.db"
    monkeypatch.setenv("UPS2MQTT_DB_PATH", str(db_path))
    repo = configure_capability_repository(Database(str(db_path)))
    repo.seed_baseline_if_needed()
    transforms_module._MISSING_SOURCE_WARNINGS.clear()
    transforms_module._UNMAPPED_VALUE_WARNINGS.clear()


def _warning_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


def test_apc_optional_bitfield_sources_skip_missing_warning_but_required_still_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="ups2mqtt")
    transforms_module.apply_catalog_transforms(
        {"battery_status": 2},
        device_uid="dev-apc-required-missing",
        runtime_source="apc_modbus_smt",
        apps_dir="/apps",
        value_cache={},
    )

    messages = _warning_messages(caplog)
    assert "Transform skipped (missing source): output_source_text" in messages
    assert "Transform skipped (missing source): ups_output_off_state" not in messages
    assert "Transform skipped (missing source): ups_on_bypass_state" not in messages
    assert "Transform skipped (missing source): ups_on_battery_state" not in messages
    assert "Transform skipped (missing source): ups_online_state" not in messages
    assert "Transform skipped (missing source): ups_low_battery_state" not in messages


def test_apc_known_enum_values_and_ignored_bitfields_do_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="ups2mqtt")
    result = transforms_module.apply_catalog_transforms(
        {
            "output_source": "normal",
            "battery_status": "battery_normal",
            "ups_status_bf": 1,
            "battery_system_error_bf": 0,
            "power_system_error_bf": 0,
            "input_status_bf": 0,
            "general_error_bf": 0,
            "bypass_input_status_bf": 0,
        },
        device_uid="dev-apc-known-values",
        runtime_source="apc_modbus_smt",
        apps_dir="/apps",
        value_cache={},
    )

    messages = _warning_messages(caplog)
    assert (
        "Transform skipped (unmapped enum): output_source_text "
        "source=output_source value='normal'"
    ) not in messages
    assert (
        "Transform skipped (unmapped enum): battery_status_text "
        "source=battery_status value='battery_normal'"
    ) not in messages
    assert (
        "Suppressing unmapped bitfield source power_system_error_bf for apc_modbus_smt"
        not in messages
    )
    assert (
        "Suppressing unmapped bitfield source input_status_bf for apc_modbus_smt"
        not in messages
    )
    assert (
        "Suppressing unmapped bitfield source general_error_bf for apc_modbus_smt"
        not in messages
    )
    assert (
        "Suppressing unmapped bitfield source bypass_input_status_bf for apc_modbus_smt"
        not in messages
    )
    assert "power_system_error_bf" not in result
    assert "input_status_bf" not in result
    assert "general_error_bf" not in result
    assert "bypass_input_status_bf" not in result


def test_unknown_enum_value_still_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="ups2mqtt")
    transforms_module.apply_catalog_transforms(
        {
            "output_source": "unexpected_mode",
            "battery_status": "battery_normal",
        },
        device_uid="dev-apc-unknown-enum",
        runtime_source="apc_modbus_smt",
        apps_dir="/apps",
        value_cache={},
    )

    messages = _warning_messages(caplog)
    assert (
        "Transform skipped (unmapped enum): output_source_text "
        "source=output_source value='unexpected_mode'"
    ) in messages


def test_cyberpower_battery_discharging_has_enum_map_and_no_suppression_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="ups2mqtt")
    result = transforms_module.apply_catalog_transforms(
        {
            "hardware_fault": 0,
            "utility_frequency_out_of_range": 0,
            "inverter_off": 0,
            "battery_not_present": 0,
            "on_battery_state": 1,
            "battery_discharging": 1,
            "battery_charging": 0,
            "battery_fully_charged": 1,
            "buzzer_muted": 0,
            "runtime_low": 0,
            "no_output": 0,
            "over_temperature": 0,
            "battery_status": "battery_normal",
        },
        device_uid="dev-cyberpower-code-map",
        runtime_source="cyberpower_modbus_single_phase",
        apps_dir="/apps",
        value_cache={},
    )

    messages = _warning_messages(caplog)
    assert (
        "Suppressing unmapped code sensor battery_discharging "
        "for cyberpower_modbus_single_phase"
    ) not in messages
    assert result.get("battery_discharging") == "on"
    assert result.get("battery_discharging_text") == "on"
