from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.capabilities import poll_group_intervals
from ups2mqtt.web import _normalize_sensor_preferences


def test_fast_poll_group_uses_device_poll_interval() -> None:
    intervals = poll_group_intervals(
        {
            "poll_groups": {
                "fast": {"interval_s": 1},
                "slow": {"interval_s": 60},
            }
        },
        default_interval=15,
    )

    assert intervals["fast"] == 15
    assert intervals["slow"] == 60


def test_poll_group_intervals_do_not_drop_below_device_interval() -> None:
    intervals = poll_group_intervals(
        {
            "poll_groups": {
                "fast": {"interval_s": 1},
                "slow": {"interval_s": 10},
                "custom": {"interval_s": 5},
            }
        },
        default_interval=15,
    )

    assert intervals["fast"] == 15
    assert intervals["slow"] == 15
    assert intervals["custom"] == 15


def test_fast_poll_group_remains_valid_sensor_choice() -> None:
    preferences = _normalize_sensor_preferences(
        {"input_voltage": {"mqtt_enabled": True, "poll_group": "fast"}},
        allowed_keys={"input_voltage"},
        allowed_poll_groups={"fast", "slow"},
    )

    assert preferences["input_voltage"]["poll_group"] == "fast"
