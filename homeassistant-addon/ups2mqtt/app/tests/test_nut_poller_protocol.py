from __future__ import annotations

from pathlib import Path
import socket
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from ups2mqtt.model import DeviceConfig
from ups2mqtt.pollers import (
    _nut_read_lines,
    _poll_apcupsd_sync,
    _poll_nut_sync,
    close_nut_keepalive_for_device,
)


class _FakeNutSocket:
    def __init__(self, responses: list[bytes], *, fail_once: bool = False) -> None:
        self._responses = list(responses)
        self._active = b""
        self._sent: list[bytes] = []
        self.fail_once = fail_once
        self.closed = False
        self.close_calls = 0

    def sendall(self, data: bytes) -> None:
        self._sent.append(data)
        if self._responses:
            self._active = self._responses.pop(0)
        else:
            self._active = b""

    def recv(self, _size: int) -> bytes:
        if self.fail_once:
            self.fail_once = False
            raise OSError("stale socket")
        if not self._active:
            return b""
        chunk = self._active
        self._active = b""
        return chunk

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


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


def test_poll_nut_sync_keep_connection_reuses_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        b"BEGIN LIST VAR devups\n"
        b'VAR devups battery.charge "100"\n'
        b"END LIST VAR devups\n"
    )
    fake_sock = _FakeNutSocket([payload, payload])
    create_calls = 0

    def _fake_connect(_addr, timeout=None):
        nonlocal create_calls
        create_calls += 1
        return fake_sock

    monkeypatch.setattr("ups2mqtt.pollers.socket.create_connection", _fake_connect)
    device = DeviceConfig(
        id="nut-keep",
        source="nut_network_upsd",
        host="127.0.0.1",
        port=3493,
        ups_name="devups",
        keep_connection_open=True,
    )
    profile = {
        "nut": {
            "variables": {
                "battery.charge": {"key": "battery_charge", "poll_group": "fast", "type": "int"}
            }
        }
    }

    first = _poll_nut_sync(device, profile, {"fast"})
    second = _poll_nut_sync(device, profile, {"fast"})
    assert first["battery_charge"] == 100
    assert second["battery_charge"] == 100
    assert create_calls == 1
    assert fake_sock.close_calls == 0

    close_nut_keepalive_for_device(device, profile)
    assert fake_sock.close_calls == 1


def test_poll_nut_sync_keep_connection_retries_one_stale_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_sock = _FakeNutSocket([], fail_once=True)
    good_sock = _FakeNutSocket(
        [
            b"BEGIN LIST VAR devups\nVAR devups ups.status \"OL\"\nEND LIST VAR devups\n",
        ]
    )
    sockets = [bad_sock, good_sock]

    def _fake_connect(_addr, timeout=None):
        return sockets.pop(0)

    monkeypatch.setattr("ups2mqtt.pollers.socket.create_connection", _fake_connect)
    device = DeviceConfig(
        id="nut-retry",
        source="nut_network_upsd",
        host="127.0.0.1",
        port=3493,
        ups_name="devups",
        keep_connection_open=True,
    )
    profile = {
        "nut": {
            "variables": {
                "ups.status": {"key": "ups_status", "poll_group": "fast", "type": "str"}
            }
        }
    }

    out = _poll_nut_sync(device, profile, {"fast"})
    assert out["ups_status"] == "OL"
    assert bad_sock.close_calls == 1
    close_nut_keepalive_for_device(device, profile)
    assert good_sock.close_calls == 1


def test_poll_nut_keepalive_same_endpoint_different_devices_do_not_share_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        b"BEGIN LIST VAR devups\n"
        b'VAR devups battery.charge "100"\n'
        b"END LIST VAR devups\n"
    )
    sock_a = _FakeNutSocket([payload, payload])
    sock_b = _FakeNutSocket([payload, payload])
    sockets = [sock_a, sock_b]

    def _fake_connect(_addr, timeout=None):
        return sockets.pop(0)

    monkeypatch.setattr("ups2mqtt.pollers.socket.create_connection", _fake_connect)
    profile = {
        "nut": {
            "variables": {
                "battery.charge": {"key": "battery_charge", "poll_group": "fast", "type": "int"}
            }
        }
    }
    device_a = DeviceConfig(
        id="nut-a",
        source="nut_network_upsd",
        host="127.0.0.1",
        port=3493,
        ups_name="devups",
        keep_connection_open=True,
        device_uid="uid-a",
    )
    device_b = DeviceConfig(
        id="nut-b",
        source="nut_network_upsd",
        host="127.0.0.1",
        port=3493,
        ups_name="devups",
        keep_connection_open=True,
        device_uid="uid-b",
    )

    _poll_nut_sync(device_a, profile, {"fast"})
    _poll_nut_sync(device_b, profile, {"fast"})
    _poll_nut_sync(device_a, profile, {"fast"})
    _poll_nut_sync(device_b, profile, {"fast"})

    assert sock_a.close_calls == 0
    assert sock_b.close_calls == 0
    close_nut_keepalive_for_device(device_a, profile)
    assert sock_a.close_calls == 1
    assert sock_b.close_calls == 0
    close_nut_keepalive_for_device(device_b, profile)
    assert sock_b.close_calls == 1


def test_poll_nut_keepalive_same_endpoint_different_ups_names_do_not_share_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_a = (
        b"BEGIN LIST VAR devups-a\n"
        b'VAR devups-a battery.charge "100"\n'
        b"END LIST VAR devups-a\n"
    )
    payload_b = (
        b"BEGIN LIST VAR devups-b\n"
        b'VAR devups-b battery.charge "99"\n'
        b"END LIST VAR devups-b\n"
    )
    sock_a = _FakeNutSocket([payload_a])
    sock_b = _FakeNutSocket([payload_b])
    sockets = [sock_a, sock_b]

    def _fake_connect(_addr, timeout=None):
        return sockets.pop(0)

    monkeypatch.setattr("ups2mqtt.pollers.socket.create_connection", _fake_connect)
    profile = {
        "nut": {
            "variables": {
                "battery.charge": {"key": "battery_charge", "poll_group": "fast", "type": "int"}
            }
        }
    }
    device_a = DeviceConfig(
        id="nut-a",
        source="nut_network_upsd",
        host="127.0.0.1",
        port=3493,
        ups_name="devups-a",
        keep_connection_open=True,
    )
    device_b = DeviceConfig(
        id="nut-b",
        source="nut_network_upsd",
        host="127.0.0.1",
        port=3493,
        ups_name="devups-b",
        keep_connection_open=True,
    )

    out_a = _poll_nut_sync(device_a, profile, {"fast"})
    out_b = _poll_nut_sync(device_b, profile, {"fast"})
    assert out_a["battery_charge"] == 100
    assert out_b["battery_charge"] == 99

    close_nut_keepalive_for_device(device_a, profile)
    close_nut_keepalive_for_device(device_b, profile)
