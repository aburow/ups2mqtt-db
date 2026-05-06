from __future__ import annotations

from pathlib import Path
import socket
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from ups2mqtt.model import DeviceConfig
from ups2mqtt.pollers import _nut_read_lines, _poll_apcupsd_sync, _poll_nut_sync


def test_nut_read_lines_sends_list_var_before_reading_banner() -> None:
    server_sock, client_sock = socket.socketpair()
    server_sock.settimeout(1.0)
    client_sock.settimeout(1.0)
    received: list[bytes] = []

    def _server() -> None:
        data = b""
        while not data.endswith(b"\n"):
            data += server_sock.recv(1024)
        received.append(data)
        server_sock.sendall(b"BEGIN LIST VAR nutdev1\n")
        server_sock.sendall(b'VAR nutdev1 battery.charge "100"\n')
        server_sock.sendall(b"END LIST VAR nutdev1\n")
        server_sock.close()

    thread = threading.Thread(target=_server, daemon=True)
    thread.start()
    try:
        lines = _nut_read_lines(client_sock, "nutdev1")
    finally:
        client_sock.close()
    thread.join(timeout=1.0)

    assert received == [b"LIST VAR nutdev1\n"]
    assert lines[0] == "BEGIN LIST VAR nutdev1"
    assert lines[-1] == "END LIST VAR nutdev1"
    assert any(line.startswith("VAR nutdev1 battery.charge") for line in lines)


def test_nut_read_lines_timeout_does_not_return_poisoned_read_error() -> None:
    server_sock, client_sock = socket.socketpair()
    server_sock.settimeout(1.0)
    client_sock.settimeout(0.2)

    def _server() -> None:
        # Read command and intentionally send nothing so client read times out.
        data = b""
        while not data.endswith(b"\n"):
            data += server_sock.recv(1024)
        time.sleep(0.5)
        server_sock.close()

    thread = threading.Thread(target=_server, daemon=True)
    thread.start()
    try:
        with pytest.raises(TimeoutError) as err:
            _nut_read_lines(client_sock, "nutdev1")
    finally:
        client_sock.close()
    thread.join(timeout=1.0)

    assert "timed out" in str(err.value).lower()
    assert "cannot read from timed out object" not in str(err.value).lower()


def test_poll_nut_sync_surfaces_err_response(monkeypatch: pytest.MonkeyPatch) -> None:
    server_sock, client_sock = socket.socketpair()
    server_sock.settimeout(1.0)
    client_sock.settimeout(1.0)

    def _server() -> None:
        data = b""
        while not data.endswith(b"\n"):
            data += server_sock.recv(1024)
        server_sock.sendall(b"ERR UNKNOWN-UPS\n")
        server_sock.close()

    thread = threading.Thread(target=_server, daemon=True)
    thread.start()

    def _fake_connect(addr, timeout):
        return client_sock

    monkeypatch.setattr("ups2mqtt.pollers.socket.create_connection", _fake_connect)
    device = DeviceConfig(
        id="device-1",
        source="nut_network_upsd",
        host="127.0.0.1",
        port=3493,
        ups_name="nutdev1",
    )
    profile = {
        "nut": {
            "variables": {
                "battery.charge": {
                    "key": "battery_charge",
                    "poll_group": "fast",
                    "type": "float",
                }
            }
        }
    }

    with pytest.raises(OSError) as err:
        _poll_nut_sync(device, profile, {"fast"})

    thread.join(timeout=1.0)
    assert "NUT server returned error: ERR UNKNOWN-UPS" in str(err.value)


def test_poll_nut_sync_emits_selected_raw_dotted_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_sock, client_sock = socket.socketpair()
    server_sock.settimeout(1.0)
    client_sock.settimeout(1.0)

    def _server() -> None:
        data = b""
        while not data.endswith(b"\n"):
            data += server_sock.recv(1024)
        server_sock.sendall(b"BEGIN LIST VAR nutdev1\n")
        server_sock.sendall(b'VAR nutdev1 battery.voltage "27.4"\n')
        server_sock.sendall(b"END LIST VAR nutdev1\n")
        server_sock.close()

    thread = threading.Thread(target=_server, daemon=True)
    thread.start()

    def _fake_connect(addr, timeout):
        return client_sock

    monkeypatch.setattr("ups2mqtt.pollers.socket.create_connection", _fake_connect)
    device = DeviceConfig(
        id="device-1",
        source="nut_network_upsd",
        host="127.0.0.1",
        port=3493,
        ups_name="nutdev1",
    )
    profile = {
        "nut": {
            "variables": {
                "battery.voltage": {
                    "key": "battery.voltage",
                    "poll_group": "slow",
                    "type": "str",
                },
                "battery.charge": {
                    "key": "battery_charge",
                    "poll_group": "fast",
                    "type": "float",
                },
            },
            "status_map": {},
        }
    }

    out = _poll_nut_sync(device, profile, {"slow"})
    thread.join(timeout=1.0)

    assert out["battery.voltage"] == "27.4"
    assert "battery_charge" not in out


def test_poll_nut_sync_emits_selected_raw_pdu_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_sock, client_sock = socket.socketpair()
    server_sock.settimeout(1.0)
    client_sock.settimeout(1.0)

    def _server() -> None:
        data = b""
        while not data.endswith(b"\n"):
            data += server_sock.recv(1024)
        server_sock.sendall(b"BEGIN LIST VAR apc_pdu1\n")
        server_sock.sendall(b'VAR apc_pdu1 input.current "1.80"\n')
        server_sock.sendall(b'VAR apc_pdu1 outlet.count "0"\n')
        server_sock.sendall(b"END LIST VAR apc_pdu1\n")
        server_sock.close()

    thread = threading.Thread(target=_server, daemon=True)
    thread.start()

    def _fake_connect(addr, timeout):
        return client_sock

    monkeypatch.setattr("ups2mqtt.pollers.socket.create_connection", _fake_connect)
    device = DeviceConfig(
        id="device-1",
        source="nut_network_upsd",
        host="127.0.0.1",
        port=3493,
        ups_name="apc_pdu1",
    )
    profile = {
        "nut": {
            "variables": {
                "input.current": {
                    "key": "input.current",
                    "poll_group": "fast",
                    "type": "str",
                },
                "outlet.count": {
                    "key": "outlet.count",
                    "poll_group": "fast",
                    "type": "str",
                },
            },
            "status_map": {},
        }
    }

    out = _poll_nut_sync(device, profile, {"fast"})
    thread.join(timeout=1.0)

    assert out["input.current"] == "1.80"
    assert out["outlet.count"] == "0"


def test_poll_apcupsd_sync_parses_status_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ups2mqtt.pollers.get_apcupsd_status",
        lambda **kwargs: {
            "APC": "001,051,1306",
            "LINEV": "238.0 Volts",
            "BCHARGE": "100.0 Percent",
            "TIMELEFT": "24.0 Minutes",
            "VENDORX": "custom",
        },
    )
    device = DeviceConfig(
        id="device-1",
        source="apcupsd_network_nis",
        host="127.0.0.1",
        port=3551,
    )
    profile = {
        "apcupsd": {
            "fields": {
                "LINEV": {
                    "key": "input_voltage",
                    "poll_group": "fast",
                    "type": "float",
                },
                "BCHARGE": {
                    "key": "battery_charge",
                    "poll_group": "fast",
                    "type": "float",
                },
                "TIMELEFT": {
                    "key": "runtime_remaining",
                    "poll_group": "fast",
                    "type": "float",
                },
                "VENDORX": {"key": "VENDORX", "poll_group": "slow", "type": "str"},
            }
        }
    }
    out = _poll_apcupsd_sync(device, profile, {"fast"})

    assert out["input_voltage"] == 238.0
    assert out["battery_charge"] == 100.0
    assert out["runtime_remaining"] == 24.0
    assert "VENDORX" not in out
