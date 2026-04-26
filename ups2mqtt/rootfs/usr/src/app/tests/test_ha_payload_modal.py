from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.database import Database
from ups2mqtt.log_buffer import LogBuffer
from ups2mqtt.model import DeviceConfig
from ups2mqtt.store import DeviceStore
from ups2mqtt.web import start_web_server


def _fetch(
    base_url: str, path: str, headers: dict[str, str] | None = None
) -> tuple[int, str]:
    request = Request(f"{base_url}{path}", headers=headers or {})
    try:
        with urlopen(request) as response:  # nosec B310
            return int(response.status), response.read().decode("utf-8")
    except HTTPError as err:
        return int(err.code), err.read().decode("utf-8")


def _start_test_server(
    *,
    tmp_path: Path,
    devices: list[DeviceConfig],
    preview_callback,
):
    db = Database(str(tmp_path / "test.db"))
    store = DeviceStore(devices, db)
    callback_calls = {
        "republish_discovery": 0,
        "reload": 0,
    }

    def _republish_discovery() -> None:
        callback_calls["republish_discovery"] += 1

    def _reload() -> None:
        callback_calls["reload"] += 1

    server = start_web_server(
        host="127.0.0.1",
        port=0,
        store=store,
        get_source_names=lambda: ["cyberpower_modbus_single_phase"],
        log_buffer=LogBuffer(),
        get_capability_status=lambda: {},
        trigger_capability_reload=lambda: None,
        trigger_republish_discovery=_republish_discovery,
        get_metrics_snapshot=lambda: {},
        trigger_reload=_reload,
        get_cached_ha_payload_preview=preview_callback,
    )
    return server, callback_calls


def test_devices_table_renders_ha_payload_button_for_each_device(tmp_path: Path) -> None:
    devices = [
        DeviceConfig(
            id="ups-a",
            source="cyberpower_modbus_single_phase",
            host="10.0.0.10",
            device_uid="uid-ups-a",
        ),
        DeviceConfig(
            id="ups-b",
            source="cyberpower_modbus_single_phase",
            host="10.0.0.11",
            device_uid="uid-ups-b",
        ),
    ]
    server, _ = _start_test_server(
        tmp_path=tmp_path,
        devices=devices,
        preview_callback=lambda device: {},
    )
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(base_url, "/htmx/devices/partials/table")
        assert status == HTTPStatus.OK
        assert body.count("HA Payload") == 2
        assert (
            'hx-get="/htmx/devices/partials/modal/ha-payload?id=ups-a"' in body
        )
        assert (
            'hx-get="/htmx/devices/partials/modal/ha-payload?id=ups-b"' in body
        )
        assert 'hx-target="#device-modal-content"' in body
        assert '@click="modalOpen = true"' in body
    finally:
        server.shutdown()
        server.server_close()


def test_ha_payload_modal_known_device_renders_readable_cached_html(tmp_path: Path) -> None:
    device = DeviceConfig(
        id="ups-1",
        source="cyberpower_modbus_single_phase",
        host="10.0.0.12",
        device_uid="uid-ups-1",
        name="Server Rack UPS",
    )

    def _preview(_device: DeviceConfig) -> dict[str, object]:
        return {
            "identity": "uid-ups-1",
            "topics": {
                "state_topic": "ups2mqtt/ups-1/state",
                "availability_topic": "ups2mqtt/ups-1/availability",
                "discovery_prefix": "homeassistant",
            },
            "cached_metadata": {
                "manufacturer": "CyberPower",
                "model": "OLS3000ERT2UA",
                "api_token": "super-secret-token",
            },
            "cached_state": {
                "battery_state_of_charge": 100,
                "input_frequency": 50.0,
            },
            "entities": [
                {
                    "key": "battery_state_of_charge",
                    "discovery_topic": "homeassistant/sensor/ups2mqtt_uid-ups-1_battery_state_of_charge/config",
                }
            ],
        }

    server, calls = _start_test_server(
        tmp_path=tmp_path,
        devices=[device],
        preview_callback=_preview,
    )
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(
            base_url, "/htmx/devices/partials/modal/ha-payload?id=ups-1"
        )
        assert status == HTTPStatus.OK
        assert "Home Assistant Payload Preview" in body
        assert '<div class="card">' in body
        assert "Server Rack UPS" in body
        assert "uid-ups-1" in body
        assert "CyberPower" in body
        assert "battery_state_of_charge" in body
        assert "100" in body
        assert "***REDACTED***" in body
        assert "super-secret-token" not in body
        assert calls["republish_discovery"] == 0
        assert calls["reload"] == 0
    finally:
        server.shutdown()
        server.server_close()


def test_ha_payload_modal_known_device_with_empty_cache_renders_empty_state(
    tmp_path: Path,
) -> None:
    device = DeviceConfig(
        id="ups-empty",
        source="cyberpower_modbus_single_phase",
        host="10.0.0.13",
        device_uid="uid-ups-empty",
        name="UPS Empty",
    )
    server, calls = _start_test_server(
        tmp_path=tmp_path,
        devices=[device],
        preview_callback=lambda _device: {},
    )
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(
            base_url, "/htmx/devices/partials/modal/ha-payload?id=ups-empty"
        )
        assert status == HTTPStatus.OK
        assert "Home Assistant Payload Preview" in body
        assert "UPS Empty" in body
        assert "No cached Home Assistant payload data is available for this device yet." in body
        assert '<div class="card">' in body
        assert calls["republish_discovery"] == 0
        assert calls["reload"] == 0
    finally:
        server.shutdown()
        server.server_close()


def test_ha_payload_modal_unknown_device_htmx_request_returns_modal_message(
    tmp_path: Path,
) -> None:
    server, calls = _start_test_server(
        tmp_path=tmp_path,
        devices=[],
        preview_callback=lambda device: {},
    )
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(
            base_url,
            "/htmx/devices/partials/modal/ha-payload?id=missing-ups",
            headers={"HX-Request": "true"},
        )
        assert status == HTTPStatus.OK
        assert "was not found" in body
        assert "missing-ups" in body
        assert "Home Assistant Payload Preview" in body
        assert '<div class="card">' in body
        assert calls["republish_discovery"] == 0
        assert calls["reload"] == 0
    finally:
        server.shutdown()
        server.server_close()
