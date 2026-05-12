from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.database import Database
from ups2mqtt.log_buffer import LogBuffer
from ups2mqtt.model import AppConfig, DeviceConfig
from ups2mqtt.mqtt import MqttPublisher
from ups2mqtt.store import DeviceStore
from ups2mqtt.web import start_web_server


def _fetch(base_url: str, path: str) -> tuple[int, str, dict[str, str]]:
    request = Request(f"{base_url}{path}")
    try:
        with urlopen(request) as response:  # nosec B310
            return (
                int(response.status),
                response.read().decode("utf-8"),
                dict(response.headers.items()),
            )
    except HTTPError as err:
        return int(err.code), err.read().decode("utf-8"), dict(err.headers.items())


def _start_test_server(
    tmp_path: Path,
    *,
    get_prometheus_samples=None,
    get_metrics_snapshot=None,
    metrics_only: bool = False,
):
    db = Database(str(tmp_path / "test.db"))
    store = DeviceStore([], db)
    server = start_web_server(
        host="127.0.0.1",
        port=0,
        store=store,
        get_source_names=lambda: ["nut_network_upsd"],
        log_buffer=LogBuffer(),
        get_capability_status=lambda: {},
        trigger_capability_reload=lambda: None,
        trigger_republish_discovery=lambda: None,
        get_metrics_snapshot=(get_metrics_snapshot or (lambda: {})),
        trigger_reload=lambda: None,
        get_prometheus_samples=get_prometheus_samples,
        metrics_only=metrics_only,
    )
    return server


def test_prometheus_endpoint_returns_200_with_help_and_type_when_empty(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path, get_prometheus_samples=lambda _devices: [])
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body, headers = _fetch(base_url, "/metrics/prometheus")
        assert status == HTTPStatus.OK
        assert headers.get("Content-Type", "").startswith("text/plain")
        assert (
            "# HELP ups2mqtt_device_value Latest selected numeric value published by ups2mqtt."
            in body
        )
        assert "# TYPE ups2mqtt_device_value gauge" in body
    finally:
        server.shutdown()
        server.server_close()


def test_prometheus_endpoint_renders_numeric_and_omits_non_numeric_and_escapes_labels(
    tmp_path: Path,
) -> None:
    def _samples(_devices):
        return [
            {
                "device_id": "nutdev1",
                "source": "nut_network_upsd",
                "key": "battery.voltage",
                "value": 52.3,
            },
            {
                "device_id": "nutdev1",
                "source": "nut_network_upsd",
                "key": 'weird"key\\line\nnext',
                "value": 1.0,
            },
            {
                "device_id": "nutdev1",
                "source": "nut_network_upsd",
                "key": "ups.status",
                "value": "OL",
            },
            {
                "device_id": "nutdev1",
                "source": "nut_network_upsd",
                "key": "bool_value",
                "value": True,
            },
        ]

    server = _start_test_server(tmp_path, get_prometheus_samples=_samples)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body, _headers = _fetch(base_url, "/metrics")
        assert status == HTTPStatus.OK
        assert (
            'ups2mqtt_device_value{device_id="nutdev1",source="nut_network_upsd",key="battery.voltage"} 52.3'
            in body
        )
        assert 'key="weird\\"key\\\\line\\nnext"} 1' in body
        assert "ups.status" not in body
        assert "bool_value" not in body
        assert "snmp_community" not in body
        assert "host=" not in body
        assert "port=" not in body
        assert "ups_name=" not in body
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_json_route_unchanged(tmp_path: Path) -> None:
    metrics_snapshot = {
        "backpressure": {
            "polls_in_flight": 0,
            "concurrency_limiter": {
                "available": 10,
                "current_limit": 10,
                "queued": 0,
            },
            "adaptive_concurrency": {
                "available": 10,
                "current_limit": 10,
                "queued": 0,
            },
        }
    }
    server = _start_test_server(
        tmp_path,
        get_prometheus_samples=lambda _devices: [],
        get_metrics_snapshot=lambda: metrics_snapshot,
    )
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body, headers = _fetch(base_url, "/metrics.json")
        assert status == HTTPStatus.OK
        assert headers.get("Content-Type", "").startswith("application/json")
        assert body.strip().startswith("{")
        parsed = json.loads(body)
        assert "concurrency_limiter" in parsed["backpressure"]
        assert "adaptive_concurrency" in parsed["backpressure"]
        assert (
            parsed["backpressure"]["concurrency_limiter"]
            == parsed["backpressure"]["adaptive_concurrency"]
        )
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_only_listener_allows_prometheus_and_blocks_ui_routes(
    tmp_path: Path,
) -> None:
    server = _start_test_server(
        tmp_path,
        get_prometheus_samples=lambda _devices: [],
        metrics_only=True,
    )
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, _headers = _fetch(base_url, "/metrics/prometheus")
        assert status == HTTPStatus.OK
        status, _body, _headers = _fetch(base_url, "/metrics")
        assert status == HTTPStatus.OK

        for blocked_path in (
            "/",
            "/htmx/devices",
            "/htmx/devices/partials/panel/profiles",
            "/metrics.json",
            "/check-config.json",
        ):
            status, _body, _headers = _fetch(base_url, blocked_path)
            assert status == HTTPStatus.NOT_FOUND
    finally:
        server.shutdown()
        server.server_close()


def test_mqtt_publish_state_continues_when_cache_update_fails() -> None:
    config = AppConfig(
        mqtt_enabled=True,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_username=None,
        mqtt_password=None,
        mqtt_discovery_prefix="homeassistant",
        mqtt_topic_prefix="ups2mqtt",
        poll_interval=15,
        poll_timeout=15,
        max_concurrent_polls=8,
        apps_dir="/tmp",
        web_enabled=True,
        web_host="127.0.0.1",
        web_port=8099,
        devices=[],
        raw={},
    )
    publisher = MqttPublisher(config)

    class _FakeClient:
        def __init__(self) -> None:
            self.published: list[tuple[str, str, int, bool]] = []

        def publish(self, topic, payload, qos=0, retain=False):
            self.published.append((topic, payload, qos, retain))
            return None

    class _FailingCache(dict):
        def get(self, key, default=None):  # noqa: ANN001
            raise RuntimeError("cache failure")

    publisher._attempt_connect = lambda force=False: True  # type: ignore[method-assign]
    publisher._client = _FakeClient()  # type: ignore[assignment]
    publisher._device_state_cache = _FailingCache()  # type: ignore[assignment]

    device = DeviceConfig(
        id="nutdev1",
        source="nut_network_upsd",
        host="192.0.2.10",
        port=3493,
    )

    published = publisher.publish_state(
        device,
        {"battery_charge": 99.0, "ups.status": "OL"},
        discovery_keys=["battery_charge", "ups.status"],
    )

    assert published is True
    assert len(publisher._client.published) >= 1  # type: ignore[attr-defined]
    state_publish = publisher._client.published[0]  # type: ignore[attr-defined]
    assert state_publish[0] == "ups2mqtt/nutdev1/state"
    assert '"battery_charge":99.0' in state_publish[1]
