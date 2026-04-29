from __future__ import annotations

from http import HTTPStatus
import http.client
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

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


def _fetch_raw(base_url: str, path: str) -> tuple[int, dict[str, str], str]:
    request = Request(f"{base_url}{path}", method="GET")
    try:
        with urlopen(request) as response:  # nosec B310
            return (
                int(response.status),
                dict(response.headers.items()),
                response.read().decode("utf-8"),
            )
    except HTTPError as err:
        return int(err.code), dict(err.headers.items()), err.read().decode("utf-8")


def _fetch_no_redirect(port: int, path: str) -> tuple[int, dict[str, str], str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        return int(response.status), dict(response.getheaders()), body
    finally:
        connection.close()


def _post(base_url: str, path: str, data: dict[str, str]) -> tuple[int, str]:
    encoded = urlencode(data).encode("utf-8")
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


def _start_test_server(tmp_path: Path):
    db = Database(str(tmp_path / "test.db"))
    store = DeviceStore([], db)
    server = start_web_server(
        host="127.0.0.1",
        port=0,
        store=store,
        get_source_names=lambda: ["cyberpower_modbus_single_phase"],
        log_buffer=LogBuffer(),
        get_capability_status=lambda: {},
        trigger_capability_reload=lambda: None,
        trigger_republish_discovery=lambda: None,
        get_metrics_snapshot=lambda: {},
        trigger_reload=lambda: None,
    )
    return server


def _start_test_server_with_base_path(tmp_path: Path, web_base_path: str):
    db = Database(str(tmp_path / "test.db"))
    store = DeviceStore([], db)
    server = start_web_server(
        host="127.0.0.1",
        port=0,
        store=store,
        get_source_names=lambda: ["cyberpower_modbus_single_phase"],
        log_buffer=LogBuffer(),
        get_capability_status=lambda: {},
        trigger_capability_reload=lambda: None,
        trigger_republish_discovery=lambda: None,
        get_metrics_snapshot=lambda: {},
        trigger_reload=lambda: None,
        web_base_path=web_base_path,
    )
    return server


def test_successful_get_request_does_not_emit_info_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="ups2mqtt.web")
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        caplog.clear()
        status, _ = _fetch(base_url, "/htmx/devices")
        assert status == HTTPStatus.OK
        info_messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "ups2mqtt.web" and record.levelname == "INFO"
        ]
        assert not any("GET /htmx/devices" in message for message in info_messages)
    finally:
        server.shutdown()
        server.server_close()


def test_invalid_csv_row_still_emits_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="ups2mqtt.web")
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        csv_payload = "ID,Source,Host\nbad,row"
        status, _ = _post(
            base_url,
            "/",
            {"action": "import_csv", "csv_file": csv_payload},
        )
        assert status == HTTPStatus.OK
        warning_messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "ups2mqtt.web" and record.levelname == "WARNING"
        ]
        assert any("Skipping malformed CSV line" in message for message in warning_messages)
    finally:
        server.shutdown()
        server.server_close()


def test_csv_import_exception_still_emits_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("ERROR", logger="ups2mqtt.web")
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        csv_payload = (
            "ID,Source,Host,Port,Unit,SNMP,Poll,Name,Debug,KeepConnectionOpen,Discovery,Polling\n"
            "ups-a,cyberpower_modbus_single_phase,127.0.0.1,not-int,1,public,,UPS,false,false,true,true"
        )
        status, _ = _post(
            base_url,
            "/",
            {"action": "import_csv", "csv_file": csv_payload},
        )
        assert status == HTTPStatus.OK
        error_messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "ups2mqtt.web" and record.levelname == "ERROR"
        ]
        assert any("CSV import error on row" in message for message in error_messages)
    finally:
        server.shutdown()
        server.server_close()


def test_prefixed_ingress_path_resolves_htmx_route(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(base_url, "/api/hassio_ingress/mock/htmx/devices")
        assert status == HTTPStatus.OK
        assert "ups2mqtt Admin" in body
    finally:
        server.shutdown()
        server.server_close()


def test_root_redirect_uses_configured_base_path(tmp_path: Path) -> None:
    server = _start_test_server_with_base_path(
        tmp_path, "/api/hassio_ingress/mock"
    )
    try:
        status, headers, _ = _fetch_no_redirect(server.server_port, "/")
        assert status == HTTPStatus.SEE_OTHER
        assert headers.get("Location", "").startswith(
            "/api/hassio_ingress/mock/htmx/devices"
        )
    finally:
        server.shutdown()
        server.server_close()
