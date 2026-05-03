from __future__ import annotations

import json
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.config import load_config
from ups2mqtt.constants import DEFAULT_POLL_INTERVAL_SECONDS


def _write_options(path: Path, *, ha_bridge_enabled: bool | None = None) -> None:
    payload: dict[str, object] = {
        "mqtt_enabled": True,
        "mqtt_host": "127.0.0.1",
        "mqtt_port": 1883,
        "config": "devices: []\n",
    }
    if ha_bridge_enabled is not None:
        payload["ha_bridge_enabled"] = ha_bridge_enabled
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_ha_bridge_enabled_defaults_false(tmp_path: Path, monkeypatch) -> None:
    options_path = tmp_path / "options.json"
    _write_options(options_path)
    monkeypatch.setenv("UPS2MQTT_RUNTIME_SETTINGS_PATH", str(tmp_path / "settings.yaml"))
    monkeypatch.delenv("UPS2MQTT_HA_BRIDGE_ENABLED", raising=False)

    config = load_config(str(options_path))
    assert config.ha_bridge_enabled is False


def test_ha_bridge_enabled_uses_runtime_settings_then_env_override(
    tmp_path: Path, monkeypatch
) -> None:
    options_path = tmp_path / "options.json"
    _write_options(options_path, ha_bridge_enabled=True)
    runtime_settings = tmp_path / "settings.yaml"
    runtime_settings.write_text(
        yaml.safe_dump({"ha_bridge_enabled": False}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("UPS2MQTT_RUNTIME_SETTINGS_PATH", str(runtime_settings))
    monkeypatch.delenv("UPS2MQTT_HA_BRIDGE_ENABLED", raising=False)

    config = load_config(str(options_path))
    assert config.ha_bridge_enabled is False

    monkeypatch.setenv("UPS2MQTT_HA_BRIDGE_ENABLED", "true")
    config_with_env = load_config(str(options_path))
    assert config_with_env.ha_bridge_enabled is True


def test_poll_interval_default_and_device_overrides_are_clamped(
    tmp_path: Path, monkeypatch
) -> None:
    options_path = tmp_path / "options.json"
    payload = {
        "mqtt_enabled": True,
        "mqtt_host": "127.0.0.1",
        "mqtt_port": 1883,
        "poll_interval": 10,
        "config": yaml.safe_dump(
            {
                "devices": [
                    {
                        "id": "dev-a",
                        "source": "apc_modbus_smt",
                        "host": "10.0.0.10",
                        "poll_interval": 10,
                    }
                ]
            },
            sort_keys=False,
        ),
    }
    options_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("UPS2MQTT_RUNTIME_SETTINGS_PATH", str(tmp_path / "settings.yaml"))

    config = load_config(str(options_path))

    assert config.poll_interval == DEFAULT_POLL_INTERVAL_SECONDS
    assert config.devices[0].poll_interval == DEFAULT_POLL_INTERVAL_SECONDS
