#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import socket


def _decode_nis_records(data: bytes) -> list[str]:
    records: list[str] = []
    offset = 0
    total = len(data)
    while offset + 2 <= total:
        length = int.from_bytes(data[offset : offset + 2], "big")
        offset += 2
        if length == 0:
            break
        end = min(offset + length, total)
        payload = data[offset:end]
        offset = end
        text = payload.decode("latin-1", errors="ignore").strip("\x00\r\n")
        if text:
            records.append(text)
    return records


def _parse_apcupsd_status_data(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    records = _decode_nis_records(data)
    if not records and len(data) >= 2:
        # Backward-compatible fallback for flat payloads.
        records = data[2:].decode("latin-1", errors="ignore").split("\n")
    for record in records:
        text = record.strip()
        if not text or ":" not in text:
            continue
        key, value = text.split(":", 1)
        key_text = key.strip()
        if key_text:
            out[key_text] = value.strip().strip("\x00")
    return out


def get_apcupsd_status(
    host: str = "localhost",
    port: int = 3551,
    timeout: float = 10.0,
) -> dict[str, str]:
    """Fetch APCUPSD NIS status as key/value pairs."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, int(port)))
        return get_apcupsd_status_from_socket(sock, timeout=timeout)
    finally:
        sock.close()


def get_apcupsd_status_from_socket(
    sock: socket.socket,
    *,
    timeout: float = 10.0,
) -> dict[str, str]:
    """Fetch APCUPSD NIS status over an already-connected socket."""
    sock.settimeout(timeout)
    # NIS request: 2-byte big-endian length + "status"
    sock.sendall(b"\x00\x06status")
    data = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        # apcupsd terminates response with zero-length message marker.
        if data.endswith(b"\x00\x00"):
            break

    if len(data) < 2:
        return {}
    return _parse_apcupsd_status_data(data)
