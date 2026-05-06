from __future__ import annotations

from pathlib import Path
import socket
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.vendor.apcupsd_nis import get_apcupsd_status


def _frame(record: bytes) -> bytes:
    return len(record).to_bytes(2, "big") + record


def test_get_apcupsd_status_decodes_length_prefixed_records(
    monkeypatch,
) -> None:
    server_sock, client_sock = socket.socketpair()
    server_sock.settimeout(1.0)
    client_sock.settimeout(1.0)

    def _server() -> None:
        _ = server_sock.recv(16)
        payload = b"".join(
            [
                _frame(b"STATUS   : ONLINE\n"),
                _frame(b"HOSTNAME : devbox\n"),
                _frame(b"BCHARGE  : 100.0 Percent\n"),
                b"\x00\x00",
            ]
        )
        server_sock.sendall(payload)
        server_sock.close()

    thread = threading.Thread(target=_server, daemon=True)
    thread.start()

    class _FakeSocket:
        def __init__(self, sock):
            self._sock = sock

        def settimeout(self, timeout):
            self._sock.settimeout(timeout)

        def connect(self, addr):
            return None

        def sendall(self, data):
            self._sock.sendall(data)

        def recv(self, size):
            return self._sock.recv(size)

        def close(self):
            self._sock.close()

    monkeypatch.setattr(
        "ups2mqtt.vendor.apcupsd_nis.socket.socket",
        lambda *args, **kwargs: _FakeSocket(client_sock),
    )

    out = get_apcupsd_status(host="127.0.0.1", port=3551, timeout=1.0)
    thread.join(timeout=1.0)

    assert out["STATUS"] == "ONLINE"
    assert out["HOSTNAME"] == "devbox"
    assert out["BCHARGE"] == "100.0 Percent"
    assert all("\x00" not in key for key in out)

