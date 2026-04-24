# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


DEVICE_PATTERNS = [
    re.compile(r"Device debug \[([^\]]+)\]"),
    re.compile(r"\bfor ([A-Za-z0-9_.:-]+)\b"),
]


@dataclass(slots=True)
class BufferedLogEntry:
    ts: str
    level: str
    logger: str
    message: str
    device: str | None


class LogBuffer:
    def __init__(self, capacity: int = 2000) -> None:
        self._lock = threading.Lock()
        self._entries: deque[BufferedLogEntry] = deque(maxlen=max(100, capacity))

    @staticmethod
    def _extract_device(message: str) -> str | None:
        for pattern in DEVICE_PATTERNS:
            match = pattern.search(message)
            if match:
                return match.group(1)
        return None

    def append(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        entry = BufferedLogEntry(
            ts=datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            level=record.levelname,
            logger=record.name,
            message=message,
            device=self._extract_device(message),
        )
        with self._lock:
            self._entries.append(entry)

    def query(
        self,
        *,
        level: str = "",
        logger: str = "",
        contains: str = "",
        device: str = "",
        limit: int = 200,
    ) -> list[BufferedLogEntry]:
        level = level.strip().upper()
        logger = logger.strip().lower()
        contains = contains.strip().lower()
        device = device.strip().lower()

        with self._lock:
            entries = list(self._entries)

        out: list[BufferedLogEntry] = []
        for entry in reversed(entries):
            if level and entry.level.upper() != level:
                continue
            if logger and logger not in entry.logger.lower():
                continue
            if contains and contains not in entry.message.lower():
                continue
            if device and (entry.device or "").lower() != device:
                continue
            out.append(entry)
            if len(out) >= max(1, min(2000, int(limit))):
                break
        return out


class BufferedLogHandler(logging.Handler):
    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__(level=logging.INFO)
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(record)
        except Exception:  # grain: ignore NAKED_EXCEPT
            pass
