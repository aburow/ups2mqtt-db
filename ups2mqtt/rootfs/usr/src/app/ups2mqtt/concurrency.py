# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict, deque
from contextlib import asynccontextmanager


class _LimiterWaiter:
    granted = False


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
        self._waiters: dict[str, deque[object]] = defaultdict(deque)
        self._source_order: deque[str] = deque()
        self._last_granted_source = ""
        self._adjustments = 0
        self._last_adjustment = ""
        self._lock = threading.Lock()
        self._cond = asyncio.Condition()

    def _grant_waiters_locked(self) -> None:
        while self._in_flight < self._limit and self._source_order:
            if (
                len(self._source_order) > 1
                and self._source_order[0] == self._last_granted_source
            ):
                self._source_order.rotate(-1)
            source = self._source_order.popleft()
            queue = self._waiters.get(source)
            if not queue:
                self._waiters.pop(source, None)
                continue
            waiter = queue.popleft()
            setattr(waiter, "granted", True)
            self._in_flight += 1
            self._last_granted_source = source
            if queue:
                self._source_order.append(source)
            else:
                self._waiters.pop(source, None)

    async def acquire(self, source: str = "") -> None:
        source_key = str(source or "unknown")
        waiter = _LimiterWaiter()
        async with self._cond:
            with self._lock:
                if not self._waiters[source_key]:
                    self._source_order.append(source_key)
                self._waiters[source_key].append(waiter)
                self._grant_waiters_locked()
            self._cond.notify_all()
            try:
                while not bool(getattr(waiter, "granted", False)):
                    await self._cond.wait()
            except BaseException:
                with self._lock:
                    if bool(getattr(waiter, "granted", False)):
                        if self._in_flight > 0:
                            self._in_flight -= 1
                    else:
                        queue = self._waiters.get(source_key)
                        if queue is not None:
                            try:
                                queue.remove(waiter)
                            except ValueError:
                                pass
                            if not queue:
                                self._waiters.pop(source_key, None)
                                self._source_order = deque(
                                    item for item in self._source_order if item != source_key
                                )
                    self._grant_waiters_locked()
                self._cond.notify_all()
                raise

    async def release(self) -> None:
        async with self._cond:
            with self._lock:
                if self._in_flight > 0:
                    self._in_flight -= 1
                self._grant_waiters_locked()
            self._cond.notify_all()

    @asynccontextmanager
    async def slot(self, source: str = ""):
        await self.acquire(source)
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
            queued_by_source = {
                source: len(queue)
                for source, queue in sorted(self._waiters.items())
                if len(queue) > 0
            }
            return {
                "adaptive_enabled": self._adaptive_enabled,
                "current_limit": int(self._limit),
                "configured_min": int(self._min_limit),
                "configured_max": int(self._max_limit),
                "in_flight": int(self._in_flight),
                "available": int(available),
                "queued": int(sum(queued_by_source.values())),
                "queued_by_source": queued_by_source,
                "adjustments": int(self._adjustments),
                "last_adjustment": self._last_adjustment,
            }
