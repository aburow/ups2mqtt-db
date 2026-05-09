from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.model import DeviceConfig
from ups2mqtt.pollers import (
    _poll_apcupsd_sync,
    close_all_apcupsd_keepalive_sessions,
    close_apcupsd_keepalive_for_device,
)


class _FakeSocket:
    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, int]] = []
        self.close_calls = 0

    def connect(self, endpoint: tuple[str, int]) -> None:
        self.connect_calls.append((str(endpoint[0]), int(endpoint[1])))

    def close(self) -> None:
        self.close_calls += 1


def _device(
    *,
    keep_connection_open: bool,
    device_id: str = "apcupsd-1",
    device_uid: str = "",
) -> DeviceConfig:
    return DeviceConfig(
        id=device_id,
        source="apcupsd_network_nis",
        host="192.168.101.36",
        port=3551,
        keep_connection_open=keep_connection_open,
        device_uid=device_uid,
    )


def _profile() -> dict[str, object]:
    return {
        "apcupsd": {
            "fields": {
                "LINEV": {"key": "input_voltage", "poll_group": "fast", "type": "float"}
            }
        }
    }


def test_apcupsd_non_sustained_uses_one_shot_get_status(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int, float]] = []

    def _fake_get_status(host: str, port: int, timeout: float) -> dict[str, str]:
        calls.append((host, int(port), float(timeout)))
        return {"LINEV": "238.0 Volts"}

    monkeypatch.setattr("ups2mqtt.pollers.get_apcupsd_status", _fake_get_status)
    device = _device(keep_connection_open=False)

    first = _poll_apcupsd_sync(device, _profile(), {"fast"})
    second = _poll_apcupsd_sync(device, _profile(), {"fast"})

    assert first["input_voltage"] == 238.0
    assert second["input_voltage"] == 238.0
    assert calls == [
        ("192.168.101.36", 3551, 5.0),
        ("192.168.101.36", 3551, 5.0),
    ]


def test_apcupsd_sustained_reuses_socket_and_cleanup_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    sockets: list[_FakeSocket] = []

    def _fake_socket(*_args, **_kwargs):
        sock = _FakeSocket()
        sockets.append(sock)
        return sock

    def _fake_get_from_socket(sock: _FakeSocket, *, timeout: float) -> dict[str, str]:
        assert timeout == 5.0
        return {"LINEV": "240.0 Volts"}

    monkeypatch.setattr("ups2mqtt.pollers.socket.socket", _fake_socket)
    monkeypatch.setattr(
        "ups2mqtt.pollers.get_apcupsd_status_from_socket",
        _fake_get_from_socket,
    )
    device = _device(keep_connection_open=True)

    first = _poll_apcupsd_sync(device, _profile(), {"fast"})
    second = _poll_apcupsd_sync(device, _profile(), {"fast"})

    assert first["input_voltage"] == 240.0
    assert second["input_voltage"] == 240.0
    assert len(sockets) == 1
    assert sockets[0].connect_calls == [("192.168.101.36", 3551)]
    assert sockets[0].close_calls == 0

    close_apcupsd_keepalive_for_device(device)
    assert sockets[0].close_calls == 1


def test_apcupsd_sustained_retries_once_on_stale_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    sockets: list[_FakeSocket] = []

    def _fake_socket(*_args, **_kwargs):
        sock = _FakeSocket()
        sockets.append(sock)
        return sock

    call_count = 0

    def _fake_get_from_socket(sock: _FakeSocket, *, timeout: float) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("stale socket")
        assert timeout == 5.0
        return {"LINEV": "241.0 Volts"}

    monkeypatch.setattr("ups2mqtt.pollers.socket.socket", _fake_socket)
    monkeypatch.setattr(
        "ups2mqtt.pollers.get_apcupsd_status_from_socket",
        _fake_get_from_socket,
    )
    device = _device(keep_connection_open=True)

    out = _poll_apcupsd_sync(device, _profile(), {"fast"})
    assert out["input_voltage"] == 241.0
    assert len(sockets) == 2
    assert sockets[0].close_calls == 1

    close_apcupsd_keepalive_for_device(device)
    assert sockets[1].close_calls == 1


def test_apcupsd_sustained_retry_failure_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    sockets: list[_FakeSocket] = []

    def _fake_socket(*_args, **_kwargs):
        sock = _FakeSocket()
        sockets.append(sock)
        return sock

    def _always_fail(_sock: _FakeSocket, *, timeout: float) -> dict[str, str]:
        assert timeout == 5.0
        raise OSError("read failed")

    monkeypatch.setattr("ups2mqtt.pollers.socket.socket", _fake_socket)
    monkeypatch.setattr(
        "ups2mqtt.pollers.get_apcupsd_status_from_socket",
        _always_fail,
    )
    device = _device(keep_connection_open=True)

    with pytest.raises(OSError):
        _poll_apcupsd_sync(device, _profile(), {"fast"})
    assert len(sockets) == 2
    assert sockets[0].close_calls == 1
    assert sockets[1].close_calls == 1


def test_apcupsd_close_all_sessions_closes_open_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    sockets: list[_FakeSocket] = []

    def _fake_socket(*_args, **_kwargs):
        sock = _FakeSocket()
        sockets.append(sock)
        return sock

    monkeypatch.setattr("ups2mqtt.pollers.socket.socket", _fake_socket)
    monkeypatch.setattr(
        "ups2mqtt.pollers.get_apcupsd_status_from_socket",
        lambda _sock, *, timeout: {"LINEV": "242.0 Volts"},
    )
    device = _device(keep_connection_open=True)
    _poll_apcupsd_sync(device, _profile(), {"fast"})
    assert len(sockets) == 1
    assert sockets[0].close_calls == 0

    close_all_apcupsd_keepalive_sessions()
    assert sockets[0].close_calls == 1


def test_apcupsd_same_endpoint_different_devices_do_not_share_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets: list[_FakeSocket] = []

    def _fake_socket(*_args, **_kwargs):
        sock = _FakeSocket()
        sockets.append(sock)
        return sock

    monkeypatch.setattr("ups2mqtt.pollers.socket.socket", _fake_socket)
    monkeypatch.setattr(
        "ups2mqtt.pollers.get_apcupsd_status_from_socket",
        lambda _sock, *, timeout: {"LINEV": "243.0 Volts"},
    )
    device_a = _device(
        keep_connection_open=True,
        device_id="apcupsd-a",
        device_uid="uid-a",
    )
    device_b = _device(
        keep_connection_open=True,
        device_id="apcupsd-b",
        device_uid="uid-b",
    )

    _poll_apcupsd_sync(device_a, _profile(), {"fast"})
    _poll_apcupsd_sync(device_b, _profile(), {"fast"})
    assert len(sockets) == 2
    assert sockets[0].close_calls == 0
    assert sockets[1].close_calls == 0

    close_apcupsd_keepalive_for_device(device_a)
    assert sockets[0].close_calls == 1
    assert sockets[1].close_calls == 0

    _poll_apcupsd_sync(device_b, _profile(), {"fast"})
    assert len(sockets) == 2
    close_apcupsd_keepalive_for_device(device_b)
    assert sockets[1].close_calls == 1
