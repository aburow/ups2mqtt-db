# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import math
import threading
from collections import defaultdict, deque
from contextlib import asynccontextmanager


class _LimiterWaiter:
    def __init__(self, source: str) -> None:
        self.source = source
        self.granted = False


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
        self._in_flight_by_source: dict[str, int] = defaultdict(int)
        self._grants_by_source: dict[str, int] = defaultdict(int)
        self._waiters: dict[str, deque[object]] = defaultdict(deque)
        self._source_order: deque[str] = deque()
        self._last_granted_source = ""
        self._adjustments = 0
        self._last_adjustment = ""
        self._lock = threading.Lock()
        self._cond = asyncio.Condition()

    def _active_source_count_locked(self) -> int:
        sources = set(self._waiters)
        sources.update(
            source for source, count in self._in_flight_by_source.items() if count > 0
        )
        return max(1, len(sources))

    def _fair_source_limit_locked(self) -> int:
        return max(1, int(math.ceil(self._limit / self._active_source_count_locked())))

    def _select_source_locked(self) -> str | None:
        if not self._source_order:
            return None

        fair_limit = self._fair_source_limit_locked()
        skipped: deque[str] = deque()
        fallback: str | None = None
        while self._source_order:
            source = self._source_order.popleft()
            queue = self._waiters.get(source)
            if not queue:
                self._waiters.pop(source, None)
                continue
            if fallback is None:
                fallback = source
            if (
                len(self._source_order) + len(skipped) > 0
                and source == self._last_granted_source
            ):
                skipped.append(source)
                continue
            if self._in_flight_by_source.get(source, 0) < fair_limit:
                self._source_order.extendleft(reversed(skipped))
                return source
            skipped.append(source)

        self._source_order.extend(skipped)
        if fallback is None:
            return None
        # Work-conserving fallback: if every queued source is at its current fair
        # share, use available global capacity instead of leaving it idle.
        try:
            self._source_order.remove(fallback)
        except ValueError:
            return None
        return fallback

    def _grant_waiters_locked(self) -> None:
        while self._in_flight < self._limit and self._source_order:
            source = self._select_source_locked()
            if source is None:
                break
            queue = self._waiters.get(source)
            if not queue:
                self._waiters.pop(source, None)
                continue
            waiter = queue.popleft()
            setattr(waiter, "granted", True)
            self._in_flight += 1
            self._in_flight_by_source[source] += 1
            self._grants_by_source[source] += 1
            self._last_granted_source = source
            if queue:
                self._source_order.append(source)
            else:
                self._waiters.pop(source, None)

    async def acquire(self, source: str = "") -> None:
        source_key = str(source or "unknown")
        waiter = _LimiterWaiter(source_key)
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
                        if self._in_flight_by_source[source_key] > 0:
                            self._in_flight_by_source[source_key] -= 1
                        if self._in_flight_by_source[source_key] <= 0:
                            self._in_flight_by_source.pop(source_key, None)
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

    async def release(self, source: str = "") -> None:
        source_key = str(source or "unknown")
        async with self._cond:
            with self._lock:
                if self._in_flight > 0:
                    self._in_flight -= 1
                if self._in_flight_by_source[source_key] > 0:
                    self._in_flight_by_source[source_key] -= 1
                if self._in_flight_by_source[source_key] <= 0:
                    self._in_flight_by_source.pop(source_key, None)
                self._grant_waiters_locked()
            self._cond.notify_all()

    @asynccontextmanager
    async def slot(self, source: str = ""):
        await self.acquire(source)
        try:
            yield
        finally:
            await self.release(source)

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
            in_flight_by_source = {
                source: count
                for source, count in sorted(self._in_flight_by_source.items())
                if count > 0
            }
            grants_by_source = {
                source: count
                for source, count in sorted(self._grants_by_source.items())
                if count > 0
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
                "in_flight_by_source": in_flight_by_source,
                "grants_by_source": grants_by_source,
                "fair_source_limit": int(self._fair_source_limit_locked()),
                "adjustments": int(self._adjustments),
                "last_adjustment": self._last_adjustment,
            }
