from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.config import load_config
from ups2mqtt.main import _enqueue_influx_telemetry, _sanitize_config_for_log
from ups2mqtt.model import DeviceConfig
from ups2mqtt.telemetry_influx import (
    InfluxV3TelemetryExporter,
    format_line_protocol_point,
)


def test_format_line_protocol_point_escapes_and_formats() -> None:
    line = format_line_protocol_point(
        measurement="ups2 mqtt,main",
        device_id="Kitchen UPS",
        source="ups=snmp,apc",
        field_key="battery.voltage",
        value=1234,
        timestamp_ms=1730000000000,
    )
    assert line.startswith("ups2\\ mqtt\\,main,")
    assert "device_id=Kitchen\\ UPS" in line
    assert "source=ups\\=snmp\\,apc" in line
    assert "field_key=battery.voltage" in line
    assert " value=1234 " in line


def test_enqueue_skips_non_numeric_bool_nan_inf() -> None:
    exporter = InfluxV3TelemetryExporter(
        url="http://127.0.0.1:8181",
        database="ups",
        token=None,
        measurement="ups2mqtt",
        timeout_seconds=2.0,
        queue_size=100,
        flush_interval_seconds=5.0,
        batch_max_points=10,
        accept_partial=True,
    )
    device = DeviceConfig(id="dev1", source="nut_network_upsd", host="127.0.0.1")
    exporter.enqueue_device_values(
        device=device,
        runtime_source="nut_network_upsd",
        values={
            "num_int": 1,
            "num_float": 2.5,
            "bool_val": True,
            "str_val": "OL",
            "none_val": None,
            "nan_val": float("nan"),
            "inf_val": float("inf"),
        },
    )
    points = []
    while not exporter._queue.empty():
        points.append(exporter._queue.get_nowait())
    assert len(points) == 2
    assert any("field_key=num_int" in point for point in points)
    assert any("field_key=num_float" in point for point in points)


def test_enqueue_queue_full_drops_without_blocking() -> None:
    exporter = InfluxV3TelemetryExporter(
        url="http://127.0.0.1:8181",
        database="ups",
        token=None,
        measurement="ups2mqtt",
        timeout_seconds=2.0,
        queue_size=1,
        flush_interval_seconds=5.0,
        batch_max_points=10,
        accept_partial=True,
    )
    device = DeviceConfig(id="dev1", source="nut_network_upsd", host="127.0.0.1")
    exporter.enqueue_device_values(
        device=device,
        runtime_source="nut_network_upsd",
        values={"a": 1, "b": 2},
    )
    assert exporter._drop_count >= 1
    assert exporter._queue.qsize() == 1


def test_enqueue_helper_swallows_exporter_exceptions() -> None:
    class _BrokenExporter:
        def enqueue_device_values(self, **_kwargs):  # noqa: ANN003
            raise RuntimeError("boom")

    device = DeviceConfig(id="dev1", source="nut_network_upsd", host="127.0.0.1")
    _enqueue_influx_telemetry(
        _BrokenExporter(),  # type: ignore[arg-type]
        device=device,
        runtime_source="nut_network_upsd",
        values={"battery_charge": 90.0},
    )


def test_load_config_parses_influx_fields(tmp_path: Path) -> None:
    options = {
        "mqtt_enabled": True,
        "poll_interval": 15,
        "config": "devices: []\n",
        "telemetry_influx_enabled": True,
        "telemetry_influx_url": "http://192.168.100.123:8181",
        "telemetry_influx_api": "v3",
        "telemetry_influx_database": "ups2mqtt_test",
        "telemetry_influx_token": "secret-token",
        "telemetry_influx_measurement": "ups2mqtt",
        "telemetry_influx_timeout_seconds": 1.5,
        "telemetry_influx_queue_size": 123,
        "telemetry_influx_flush_interval_seconds": 4.0,
        "telemetry_influx_batch_max_points": 50,
        "telemetry_influx_accept_partial": True,
    }
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps(options), encoding="utf-8")
    config = load_config(str(options_path))
    assert config.telemetry_influx_enabled is True
    assert config.telemetry_influx_url == "http://192.168.100.123:8181"
    assert config.telemetry_influx_api == "v3"
    assert config.telemetry_influx_database == "ups2mqtt_test"
    assert config.telemetry_influx_token == "secret-token"
    assert config.telemetry_influx_measurement == "ups2mqtt"
    assert math.isclose(config.telemetry_influx_timeout_seconds, 1.5)
    assert config.telemetry_influx_queue_size == 123
    assert math.isclose(config.telemetry_influx_flush_interval_seconds, 4.0)
    assert config.telemetry_influx_batch_max_points == 50
    assert config.telemetry_influx_accept_partial is True


def test_sanitize_config_redacts_influx_token() -> None:
    redacted = _sanitize_config_for_log(
        {
            "telemetry_influx_token": "secret-token",
            "telemetry_influx_url": "http://127.0.0.1:8181",
        }
    )
    assert redacted["telemetry_influx_token"] == "***REDACTED***"


def test_worker_posts_v3_endpoint_params_and_optional_bearer_header() -> None:
    exporter = InfluxV3TelemetryExporter(
        url="http://127.0.0.1:8181",
        database="upsdb",
        token="tok",
        measurement="ups2mqtt",
        timeout_seconds=2.0,
        queue_size=10,
        flush_interval_seconds=0.1,
        batch_max_points=5,
        accept_partial=True,
    )
    captured: dict[str, object] = {}

    class _Response:
        status = 204

        async def text(self) -> str:
            return ""

    class _Ctx:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
            return None

    class _Session:
        def post(self, url, *, params, data, headers):  # noqa: ANN001
            captured["url"] = url
            captured["params"] = params
            captured["data"] = data
            captured["headers"] = headers
            return _Ctx()

    asyncio.run(exporter._post_lines(_Session(), ["x,y=z value=1i 1"]))  # type: ignore[arg-type]
    assert captured["url"] == "http://127.0.0.1:8181/api/v3/write_lp"
    assert captured["params"] == {
        "db": "upsdb",
        "precision": "millisecond",
        "accept_partial": "true",
    }
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers.get("Authorization") == "Bearer tok"


def test_worker_failure_isolated_and_task_cancellable() -> None:
    exporter = InfluxV3TelemetryExporter(
        url="http://127.0.0.1:8181",
        database="upsdb",
        token=None,
        measurement="ups2mqtt",
        timeout_seconds=2.0,
        queue_size=10,
        flush_interval_seconds=0.05,
        batch_max_points=1,
        accept_partial=True,
    )

    async def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("write failed")

    exporter._post_lines = _boom  # type: ignore[method-assign]

    async def _run() -> None:
        device = DeviceConfig(id="d1", source="nut_network_upsd", host="127.0.0.1")
        exporter.enqueue_device_values(
            device=device,
            runtime_source="nut_network_upsd",
            values={"battery_charge": 90.0},
        )
        task = asyncio.create_task(exporter.run())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
