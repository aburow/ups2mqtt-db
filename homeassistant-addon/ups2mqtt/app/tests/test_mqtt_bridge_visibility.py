from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ups2mqtt.mqtt as mqtt_module
from ups2mqtt.model import AppConfig, DeviceConfig


class _FakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.published: list[dict[str, Any]] = []

    def username_pw_set(self, username: str, password: str = "") -> None:
        return None

    def will_set(
        self, topic: str, payload: str = "", qos: int = 0, retain: bool = False
    ) -> None:
        return None

    def connect(self, host: str, port: int, keepalive: int = 60) -> int:
        return 0

    def loop_start(self) -> None:
        return None

    def loop_stop(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def publish(
        self, topic: str, payload: str = "", qos: int = 0, retain: bool = False
    ) -> None:
        self.published.append(
            {
                "topic": topic,
                "payload": payload,
                "qos": qos,
                "retain": retain,
            }
        )


def _make_config(*, ha_bridge_enabled: bool) -> AppConfig:
    return AppConfig(
        mqtt_enabled=True,
        mqtt_host="127.0.0.1",
        mqtt_port=1883,
        mqtt_username=None,
        mqtt_password=None,
        mqtt_discovery_prefix="homeassistant",
        mqtt_topic_prefix="ups2mqtt",
        poll_interval=10,
        poll_timeout=15,
        max_concurrent_polls=8,
        apps_dir="/apps",
        web_enabled=True,
        web_host="0.0.0.0",
        web_port=8099,
        devices=[],
        raw={},
        ha_bridge_enabled=ha_bridge_enabled,
    )


def test_bridge_discovery_published_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(mqtt_module.mqtt, "Client", _FakeClient)
    publisher = mqtt_module.MqttPublisher(_make_config(ha_bridge_enabled=True))

    publisher.connect()
    published = publisher._client.published
    bridge_topic = "homeassistant/sensor/ups2mqtt_bridge/config"
    legacy_bridge_topic = "homeassistant/sensor/ups_unified_bridge/config"
    bridge_messages = [row for row in published if row["topic"] == bridge_topic]
    legacy_bridge_messages = [
        row for row in published if row["topic"] == legacy_bridge_topic
    ]

    assert bridge_messages
    assert all(row["retain"] for row in bridge_messages)
    payloads = [row["payload"] for row in bridge_messages]
    assert any(payload for payload in payloads)
    body = json.loads(next(payload for payload in payloads if payload))
    assert body["unique_id"] == "ups2mqtt_bridge"
    assert legacy_bridge_messages
    assert all(row["payload"] == "" for row in legacy_bridge_messages)


def test_bridge_discovery_cleared_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(mqtt_module.mqtt, "Client", _FakeClient)
    publisher = mqtt_module.MqttPublisher(_make_config(ha_bridge_enabled=False))

    publisher.connect()
    published = publisher._client.published
    bridge_topic = "homeassistant/sensor/ups2mqtt_bridge/config"
    legacy_bridge_topic = "homeassistant/sensor/ups_unified_bridge/config"
    bridge_messages = [row for row in published if row["topic"] == bridge_topic]
    legacy_bridge_messages = [
        row for row in published if row["topic"] == legacy_bridge_topic
    ]

    assert bridge_messages
    assert legacy_bridge_messages
    assert all(row["payload"] == "" and row["retain"] for row in bridge_messages)
    assert all(
        row["payload"] == "" and row["retain"] for row in legacy_bridge_messages
    )


def test_disabling_bridge_does_not_block_device_entity_discovery(monkeypatch) -> None:
    monkeypatch.setattr(mqtt_module.mqtt, "Client", _FakeClient)
    monkeypatch.setattr(
        mqtt_module,
        "resolve_enabled_defaults",
        lambda source, keys, apps_dir, authoritative=True: {
            key: True for key in keys
        },
    )
    monkeypatch.setattr(mqtt_module, "resolve_icon", lambda source, key, apps_dir: None)
    publisher = mqtt_module.MqttPublisher(_make_config(ha_bridge_enabled=False))
    device = DeviceConfig(
        id="ups-a",
        device_uid="uid-ups-a",
        source="cyberpower_modbus_single_phase",
        host="10.0.0.10",
    )

    assert publisher.publish_discovery(device, ["input_voltage"])
    published = publisher._client.published

    bridge_topic = "homeassistant/sensor/ups2mqtt_bridge/config"
    legacy_bridge_topic = "homeassistant/sensor/ups_unified_bridge/config"
    bridge_messages = [row for row in published if row["topic"] == bridge_topic]
    legacy_bridge_messages = [
        row for row in published if row["topic"] == legacy_bridge_topic
    ]
    assert bridge_messages
    assert legacy_bridge_messages
    assert all(row["payload"] == "" for row in bridge_messages)
    assert all(row["payload"] == "" for row in legacy_bridge_messages)

    device_topic = (
        "homeassistant/sensor/ups2mqtt_uid-ups-a_input_voltage/config"
    )
    device_messages = [row for row in published if row["topic"] == device_topic]
    assert device_messages
    assert any(row["payload"] for row in device_messages)
    legacy_device_topic = (
        "homeassistant/sensor/ups_unified_uid-ups-a_input_voltage/config"
    )
    legacy_device_messages = [
        row for row in published if row["topic"] == legacy_device_topic
    ]
    assert legacy_device_messages
    assert all(row["payload"] == "" for row in legacy_device_messages)
