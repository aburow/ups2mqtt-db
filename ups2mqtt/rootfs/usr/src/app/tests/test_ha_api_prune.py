from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt import ha_api, main as main_module
from ups2mqtt.model import AppConfig


class _FakeClientTimeout:
    def __init__(self, total: int | float | None = None) -> None:
        self.total = total


class _FakeWebSocket:
    def __init__(
        self,
        entity_registry: list[dict[str, Any]],
        device_registry: list[dict[str, Any]] | None = None,
    ) -> None:
        self._messages: list[dict[str, Any]] = [{"type": "auth_required"}]
        self._entity_registry = [dict(entry) for entry in entity_registry]
        self._device_registry = [dict(entry) for entry in (device_registry or [])]

    async def receive_json(self) -> dict[str, Any]:
        if not self._messages:
            raise RuntimeError("No queued websocket message")
        return self._messages.pop(0)

    async def send_json(self, payload: dict[str, Any]) -> None:
        message_type = payload.get("type")
        request_id = int(payload.get("id", 0))
        if message_type == "auth":
            self._messages.append({"type": "auth_ok"})
            return
        if message_type == "config/entity_registry/list":
            self._messages.append(
                {
                    "type": "result",
                    "id": request_id,
                    "success": True,
                    "result": [dict(entry) for entry in self._entity_registry],
                }
            )
            return
        if message_type == "config/entity_registry/remove":
            entity_id = str(payload.get("entity_id", "")).strip()
            self._entity_registry = [
                row
                for row in self._entity_registry
                if str(row.get("entity_id", "")).strip() != entity_id
            ]
            self._messages.append(
                {
                    "type": "result",
                    "id": request_id,
                    "success": True,
                    "result": {},
                }
            )
            return
        if message_type == "config/device_registry/list":
            self._messages.append(
                {
                    "type": "result",
                    "id": request_id,
                    "success": True,
                    "result": [dict(entry) for entry in self._device_registry],
                }
            )
            return
        if message_type == "config/device_registry/remove_config_entry":
            self._messages.append(
                {
                    "type": "result",
                    "id": request_id,
                    "success": True,
                    "result": {},
                }
            )
            return
        raise AssertionError(f"Unexpected websocket command: {message_type}")


class _FakeWsConnectContext:
    def __init__(self, ws: _FakeWebSocket) -> None:
        self._ws = ws

    async def __aenter__(self) -> _FakeWebSocket:
        return self._ws

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeClientSession:
    def __init__(self, ws: _FakeWebSocket) -> None:
        self._ws = ws

    async def __aenter__(self) -> _FakeClientSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def ws_connect(self, ws_url: str, timeout: Any = None) -> _FakeWsConnectContext:
        return _FakeWsConnectContext(self._ws)


class _FakeAioHttp:
    ClientTimeout = _FakeClientTimeout

    def __init__(self, ws: _FakeWebSocket) -> None:
        self._ws = ws

    def ClientSession(self) -> _FakeClientSession:
        return _FakeClientSession(self._ws)


def _run_prune(
    monkeypatch,
    *,
    registry: list[dict[str, Any]],
    expected_unique_ids: set[str],
) -> dict[str, Any]:
    ws = _FakeWebSocket(entity_registry=registry, device_registry=[])
    monkeypatch.setattr(ha_api, "aiohttp", _FakeAioHttp(ws))
    return asyncio.run(
        ha_api.delete_stale_ups_entities(
            "http://ha.local:8123",
            "token",
            expected_unique_ids,
            expected_device_identifiers=set(),
        )
    )


def test_prune_keeps_legacy_when_replacement_not_present(monkeypatch) -> None:
    result = _run_prune(
        monkeypatch,
        registry=[
            {
                "entity_id": "sensor.legacy_voltage",
                "unique_id": "ups_unified_device1_input_voltage",
                "device_id": "ha-device-1",
            }
        ],
        expected_unique_ids={"ups2mqtt_device1_input_voltage"},
    )
    assert result.get("deleted") == []
    assert result.get("scanned") == 0


def test_prune_keeps_expected_new_namespace_entity(monkeypatch) -> None:
    result = _run_prune(
        monkeypatch,
        registry=[
            {
                "entity_id": "sensor.new_voltage",
                "unique_id": "ups2mqtt_device1_input_voltage",
                "device_id": "ha-device-1",
            }
        ],
        expected_unique_ids={"ups2mqtt_device1_input_voltage"},
    )
    assert result.get("deleted") == []
    assert result.get("scanned") == 0


def test_prune_removes_legacy_when_replacement_exists(monkeypatch) -> None:
    result = _run_prune(
        monkeypatch,
        registry=[
            {
                "entity_id": "sensor.legacy_voltage",
                "unique_id": "ups_unified_device1_input_voltage",
                "device_id": "ha-device-1",
            },
            {
                "entity_id": "sensor.new_voltage",
                "unique_id": "ups2mqtt_device1_input_voltage",
                "device_id": "ha-device-1",
            },
        ],
        expected_unique_ids={"ups2mqtt_device1_input_voltage"},
    )
    assert result.get("deleted") == ["sensor.legacy_voltage"]
    assert result.get("scanned") == 1


def test_prune_removes_unexpected_new_namespace_entity(monkeypatch) -> None:
    result = _run_prune(
        monkeypatch,
        registry=[
            {
                "entity_id": "sensor.stale_new",
                "unique_id": "ups2mqtt_device1_stale_metric",
                "device_id": "ha-device-1",
            }
        ],
        expected_unique_ids={"ups2mqtt_device1_input_voltage"},
    )
    assert result.get("deleted") == ["sensor.stale_new"]
    assert result.get("scanned") == 1


def test_prune_expected_ids_exclude_bridge_when_disabled(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_delete(
        ha_url: str,
        ha_token: str,
        expected_unique_ids: set[str],
        expected_device_identifiers: set[str] | None = None,
    ) -> dict[str, Any]:
        captured["expected_unique_ids"] = set(expected_unique_ids)
        captured["expected_device_identifiers"] = set(
            expected_device_identifiers or set()
        )
        return {"deleted": [], "removed_devices": []}

    monkeypatch.setattr(main_module, "delete_stale_ups_entities", _fake_delete)

    config = AppConfig(
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
        adaptive_concurrency_enabled=False,
        adaptive_concurrency_min=8,
        adaptive_concurrency_max=8,
        adaptive_concurrency_window_seconds=60,
        adaptive_concurrency_target_p95_wait_ms=1000,
        apps_dir="/apps",
        web_enabled=True,
        web_host="0.0.0.0",
        web_port=8099,
        devices=[],
        raw={},
        ha_url="http://ha.local:8123",
        ha_token="token",
        ha_bridge_enabled=False,
    )
    asyncio.run(
        main_module._prune_stale_ha_entities(
            config=config,
            devices=[],
            profiles={},
            profile_bindings={},
        )
    )
    assert "ups2mqtt_bridge" not in captured["expected_unique_ids"]
    assert "ups2mqtt_bridge" not in captured["expected_device_identifiers"]
