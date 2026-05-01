# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager


class AdjustableConcurrencyLimiter:
    """Global async concurrency limiter with runtime-adjustable capacity."""

    def __init__(
        self,
        initial_limit: int,
        *,
        min_limit: int | None = None,
        max_limit: int | None = None,
        adaptive_enabled: bool = False,
    ) -> None:
        initial = max(1, int(initial_limit))
        minimum = max(1, int(min_limit if min_limit is not None else initial))
        maximum = max(minimum, int(max_limit if max_limit is not None else initial))
        self._limit = min(max(initial, minimum), maximum)
        self._min_limit = minimum
        self._max_limit = maximum
        self._adaptive_enabled = bool(adaptive_enabled)
        self._in_flight = 0
        self._adjustments = 0
        self._last_adjustment = ""
        self._lock = threading.Lock()
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._cond:
            while True:
                with self._lock:
                    if self._in_flight < self._limit:
                        self._in_flight += 1
                        return
                await self._cond.wait()

    async def release(self) -> None:
        async with self._cond:
            with self._lock:
                if self._in_flight > 0:
                    self._in_flight -= 1
            self._cond.notify_all()

    @asynccontextmanager
    async def slot(self):
        await self.acquire()
        try:
            yield
        finally:
            await self.release()

    async def __aenter__(self) -> AdjustableConcurrencyLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()

    async def set_limit(self, value: int, *, reason: str = "") -> int:
        async with self._cond:
            with self._lock:
                new_limit = min(max(int(value), self._min_limit), self._max_limit)
                if new_limit != self._limit:
                    self._limit = new_limit
                    self._adjustments += 1
                    self._last_adjustment = reason[:200]
            self._cond.notify_all()
        return self.current_limit

    @property
    def current_limit(self) -> int:
        with self._lock:
            return int(self._limit)

    @property
    def adaptive_enabled(self) -> bool:
        return self._adaptive_enabled

    def snapshot(self) -> dict[str, int | bool | str]:
        with self._lock:
            available = max(0, self._limit - self._in_flight)
            return {
                "adaptive_enabled": self._adaptive_enabled,
                "current_limit": int(self._limit),
                "configured_min": int(self._min_limit),
                "configured_max": int(self._max_limit),
                "in_flight": int(self._in_flight),
                "available": int(available),
                "adjustments": int(self._adjustments),
                "last_adjustment": self._last_adjustment,
            }
