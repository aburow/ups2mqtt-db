from __future__ import annotations

from http import HTTPStatus
import logging
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.database import Database
from ups2mqtt.log_buffer import LogBuffer
from ups2mqtt.store import DeviceStore
from ups2mqtt.web import start_web_server


def _fetch(base_url: str, path: str) -> tuple[int, str]:
    request = Request(f"{base_url}{path}")
    try:
        with urlopen(request) as response:  # nosec B310
            return int(response.status), response.read().decode("utf-8")
    except HTTPError as err:
        return int(err.code), err.read().decode("utf-8")


def _post(base_url: str, path: str, data: dict[str, str] | None = None) -> tuple[int, str]:
    encoded = urlencode(data or {}).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request) as response:  # nosec B310
            return int(response.status), response.read().decode("utf-8")
    except HTTPError as err:
        return int(err.code), err.read().decode("utf-8")


def _append_log(log_buffer: LogBuffer, message: str, level: int = logging.INFO) -> None:
    record = logging.LogRecord(
        name="ups2mqtt.tests",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    log_buffer.append(record)


def _start_test_server(tmp_path: Path, log_buffer: LogBuffer):
    db = Database(str(tmp_path / "test.db"))
    store = DeviceStore([], db)
    server = start_web_server(
        host="127.0.0.1",
        port=0,
        store=store,
        get_source_names=lambda: ["cyberpower_modbus_single_phase"],
        log_buffer=log_buffer,
        get_capability_status=lambda: {},
        trigger_capability_reload=lambda: None,
        trigger_republish_discovery=lambda: None,
        get_metrics_snapshot=lambda: {},
        trigger_reload=lambda: None,
    )
    return server


def test_logs_panel_shows_usage_and_capacity(tmp_path: Path) -> None:
    log_buffer = LogBuffer()
    _append_log(log_buffer, "first")
    _append_log(log_buffer, "second")
    server = _start_test_server(tmp_path, log_buffer)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(base_url, "/htmx/devices/partials/panel/logs")
        assert status == HTTPStatus.OK
        assert "Logs: 2 / 2000" in body
    finally:
        server.shutdown()
        server.server_close()


def test_clear_logs_action_empties_buffer_and_refreshes_panel(tmp_path: Path) -> None:
    log_buffer = LogBuffer()
    _append_log(log_buffer, "one")
    _append_log(log_buffer, "two")
    server = _start_test_server(tmp_path, log_buffer)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(base_url, "/htmx/logs/actions/clear")
        assert status == HTTPStatus.OK
        assert log_buffer.count() == 0
        assert "Logs: 0 / 2000" in body
        assert "No log entries for current filter." in body
    finally:
        server.shutdown()
        server.server_close()


def test_repeated_clear_logs_action_is_safe(tmp_path: Path) -> None:
    log_buffer = LogBuffer()
    _append_log(log_buffer, "one")
    server = _start_test_server(tmp_path, log_buffer)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        first_status, _ = _post(base_url, "/htmx/logs/actions/clear")
        second_status, second_body = _post(base_url, "/htmx/logs/actions/clear")
        assert first_status == HTTPStatus.OK
        assert second_status == HTTPStatus.OK
        assert log_buffer.count() == 0
        assert "Logs: 0 / 2000" in second_body
    finally:
        server.shutdown()
        server.server_close()


def test_legacy_clear_logs_route_alias_still_works(tmp_path: Path) -> None:
    log_buffer = LogBuffer()
    _append_log(log_buffer, "one")
    server = _start_test_server(tmp_path, log_buffer)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(base_url, "/htmx/devices/actions/logs/clear")
        assert status == HTTPStatus.OK
        assert log_buffer.count() == 0
        assert "Logs: 0 / 2000" in body
    finally:
        server.shutdown()
        server.server_close()
