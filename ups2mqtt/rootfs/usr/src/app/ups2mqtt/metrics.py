# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from .database import Database


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(slots=True)
class DeviceMetrics:
    polls_started: int = 0
    polls_succeeded: int = 0
    polls_failed: int = 0
    polls_timed_out: int = 0
    total_duration_ms: float = 0.0
    average_duration_ms: float = 0.0
    min_duration_ms: float | None = None
    max_duration_ms: float | None = None
    last_duration_ms: float | None = None
    last_wait_ms: float | None = None
    last_poll_ms: float | None = None
    last_publish_ms: float | None = None
    last_status: str = "unknown"
    last_error: str = ""
    last_update_utc: str = ""
    last_success_utc: str = ""
    last_values_count: int = 0
    cadence_count: int = 0
    cadence_total_ms: float = 0.0
    cadence_average_ms: float = 0.0
    cadence_min_ms: float | None = None
    cadence_max_ms: float | None = None
    cadence_last_ms: float | None = None


@dataclass(slots=True)
class SourceMetrics:
    polls_queued: int = 0
    polls_dequeued: int = 0
    polls_completed: int = 0
    polls_failed: int = 0
    polls_timed_out: int = 0
    total_wait_ms: float = 0.0
    average_wait_ms: float = 0.0
    last_wait_ms: float | None = None
    max_wait_ms: float | None = None
    wait_p50_ms: float = 0.0
    wait_p95_ms: float = 0.0
    endpoint_wait_p95_ms: float = 0.0
    max_queue_age_ms: float = 0.0
    active: int = 0
    queued: int = 0


@dataclass(slots=True)
class ActivePoll:
    source: str
    state: str = "queued"


class MetricsStore:
    def __init__(
        self,
        global_poll_semaphore: asyncio.Semaphore | Any | None = None,
        db: Database | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, DeviceMetrics] = {}
        self._device_sources: dict[str, str] = {}
        self._sources: dict[str, SourceMetrics] = {}
        self._last_start_monotonic: dict[str, float] = {}
        self._wait_samples: deque[tuple[float, float]] = deque(maxlen=10000)
        self._source_wait_samples: dict[str, deque[tuple[float, float]]] = {}
        self._source_endpoint_wait_samples: dict[str, deque[tuple[float, float]]] = {}
        self._source_queue_started: dict[str, deque[float]] = {}
        self._active_polls_by_device: dict[str, deque[ActivePoll]] = {}
        self._device_duration_samples: dict[str, deque[tuple[float, float]]] = {}
        self._device_wait_samples: dict[str, deque[tuple[float, float]]] = {}
        self.polls_in_flight = 0
        self.global_poll_semaphore = global_poll_semaphore
        self._db = db

    def _ensure(self, device_id: str) -> DeviceMetrics:
        if device_id not in self._devices:
            self._devices[device_id] = DeviceMetrics()
        return self._devices[device_id]

    def _ensure_source(self, source: str) -> SourceMetrics:
        key = str(source or "unknown")
        if key not in self._sources:
            self._sources[key] = SourceMetrics()
        return self._sources[key]

    def _record_wait_sample_locked(self, wait_ms: float | None) -> None:
        if wait_ms is None:
            return
        self._wait_samples.append((monotonic(), max(0.0, float(wait_ms))))

    @staticmethod
    def _record_device_sample_locked(
        samples: dict[str, deque[tuple[float, float]]],
        device_id: str,
        value_ms: float | None,
    ) -> None:
        if value_ms is None:
            return
        bucket = samples.setdefault(device_id, deque(maxlen=2000))
        bucket.append((monotonic(), max(0.0, float(value_ms))))

    @staticmethod
    def _rolling_average_from_samples(
        samples: deque[tuple[float, float]],
        now_monotonic: float,
        window_seconds: int,
    ) -> float:
        cutoff = now_monotonic - window_seconds
        values = [value for ts, value in samples if ts >= cutoff]
        if not values:
            return 0.0
        return float(sum(values) / len(values))

    def _device_load_average_locked(
        self,
        sample_map: dict[str, deque[tuple[float, float]]],
        device_id: str,
        now_monotonic: float,
    ) -> dict[str, float]:
        samples = sample_map.get(device_id, deque())
        if samples:
            cutoff = now_monotonic - 900.0
            while samples and samples[0][0] < cutoff:
                samples.popleft()
        return {
            "1m": self._rolling_average_from_samples(samples, now_monotonic, 60),
            "5m": self._rolling_average_from_samples(samples, now_monotonic, 300),
            "15m": self._rolling_average_from_samples(samples, now_monotonic, 900),
        }

    @staticmethod
    def _percentiles(values: list[float]) -> tuple[float, float, float]:
        if not values:
            return 0.0, 0.0, 0.0
        ordered = sorted(values)
        p50_index = min(len(ordered) - 1, int((len(ordered) - 1) * 0.50))
        p95_index = min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))
        return float(ordered[p50_index]), float(ordered[p95_index]), float(ordered[-1])

    def _wait_pressure_locked(
        self,
        window_seconds: int,
        sample_cap_ms: float | None = None,
    ) -> dict[str, float | int]:
        cutoff = monotonic() - max(1, int(window_seconds))
        while self._wait_samples and self._wait_samples[0][0] < cutoff:
            self._wait_samples.popleft()
        values = [
            min(wait_ms, float(sample_cap_ms))
            if sample_cap_ms is not None and sample_cap_ms > 0
            else wait_ms
            for _sample_time, wait_ms in self._wait_samples
        ]
        if not values:
            return {
                "samples": 0,
                "window_seconds": int(window_seconds),
                "p50_wait_ms": 0.0,
                "p95_wait_ms": 0.0,
                "max_wait_ms": 0.0,
            }
        p50_wait_ms, p95_wait_ms, max_wait_ms = self._percentiles(values)
        return {
            "samples": len(values),
            "window_seconds": int(window_seconds),
            "p50_wait_ms": p50_wait_ms,
            "p95_wait_ms": p95_wait_ms,
            "max_wait_ms": max_wait_ms,
        }

    def wait_pressure(
        self,
        window_seconds: int,
        sample_cap_ms: float | None = None,
    ) -> dict[str, float | int]:
        with self._lock:
            return self._wait_pressure_locked(window_seconds, sample_cap_ms)

    def source_totals(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                source: {
                    "polls_completed": int(metric.polls_completed),
                    "polls_failed": int(metric.polls_failed),
                    "polls_timed_out": int(metric.polls_timed_out),
                }
                for source, metric in self._sources.items()
            }

    def record_start(self, device_id: str, source: str = "") -> None:
        now_monotonic = monotonic()
        source_key = str(source or "unknown")
        with self._lock:
            metric = self._ensure(device_id)
            source_metric = self._ensure_source(source_key)
            self._device_sources[device_id] = source_key
            last_started = self._last_start_monotonic.get(device_id)
            if last_started is not None and now_monotonic >= last_started:
                cadence_ms = max(0.0, (now_monotonic - last_started) * 1000.0)
                metric.cadence_count += 1
                metric.cadence_total_ms += cadence_ms
                metric.cadence_average_ms = (
                    metric.cadence_total_ms / metric.cadence_count
                )
                metric.cadence_last_ms = cadence_ms
                if (
                    metric.cadence_min_ms is None
                    or cadence_ms < metric.cadence_min_ms
                ):
                    metric.cadence_min_ms = cadence_ms
                if (
                    metric.cadence_max_ms is None
                    or cadence_ms > metric.cadence_max_ms
                ):
                    metric.cadence_max_ms = cadence_ms
            self._last_start_monotonic[device_id] = now_monotonic
            metric.polls_started += 1
            metric.last_status = "running"
            metric.last_update_utc = _utc_now()
            self.polls_in_flight += 1
            self._active_polls_by_device.setdefault(device_id, deque()).append(
                ActivePoll(source=source_key)
            )
            source_metric.polls_queued += 1
            source_metric.queued += 1
            self._source_queue_started.setdefault(source_key, deque()).append(
                now_monotonic
            )

    def _consume_active_poll_locked(self, device_id: str) -> ActivePoll | None:
        active = self._active_polls_by_device.get(device_id)
        if not active:
            return None
        poll = active.popleft()
        if not active:
            self._active_polls_by_device.pop(device_id, None)
        if self.polls_in_flight > 0:
            self.polls_in_flight -= 1
        return poll

    def _discard_active_polls_locked(self, device_id: str) -> None:
        active = self._active_polls_by_device.pop(device_id, deque())
        for poll in active:
            if self.polls_in_flight > 0:
                self.polls_in_flight -= 1
            source_metric = self._ensure_source(poll.source)
            if poll.state == "active" and source_metric.active > 0:
                source_metric.active -= 1
            elif source_metric.queued > 0:
                source_metric.queued -= 1

    def record_dequeue(
        self,
        device_id: str,
        source: str = "",
        wait_ms: float | None = None,
        endpoint_wait_ms: float | None = None,
    ) -> None:
        source_key = str(source or self._device_sources.get(device_id) or "unknown")
        now_monotonic = monotonic()
        with self._lock:
            active_poll: ActivePoll | None = None
            for poll in self._active_polls_by_device.get(device_id, ()):
                if poll.source == source_key and poll.state == "queued":
                    active_poll = poll
                    poll.state = "active"
                    break
            if active_poll is None:
                return
            source_metric = self._ensure_source(source_key)
            source_metric.polls_dequeued += 1
            source_metric.active += 1
            if source_metric.queued > 0:
                source_metric.queued -= 1
            started_queue = self._source_queue_started.setdefault(
                source_key, deque()
            )
            while started_queue and now_monotonic - started_queue[0] > 3600:
                started_queue.popleft()
            if started_queue:
                age_ms = max(0.0, (now_monotonic - started_queue.popleft()) * 1000.0)
                source_metric.max_queue_age_ms = max(source_metric.max_queue_age_ms, age_ms)
            if wait_ms is not None:
                wait_value = max(0.0, float(wait_ms))
                source_metric.last_wait_ms = wait_value
                source_metric.max_wait_ms = (
                    wait_value
                    if source_metric.max_wait_ms is None
                    else max(source_metric.max_wait_ms, wait_value)
                )
                samples = self._source_wait_samples.setdefault(
                    source_key, deque(maxlen=1000)
                )
                samples.append((now_monotonic, wait_value))
                recent = [
                    value
                    for sample_time, value in samples
                    if now_monotonic - sample_time <= 60.0
                ]
                source_metric.wait_p50_ms, source_metric.wait_p95_ms, _ = (
                    self._percentiles(recent)
                )
            if endpoint_wait_ms is not None:
                endpoint_value = max(0.0, float(endpoint_wait_ms))
                endpoint_samples = self._source_endpoint_wait_samples.setdefault(
                    source_key, deque(maxlen=1000)
                )
                endpoint_samples.append((now_monotonic, endpoint_value))
                recent_endpoint = [
                    value
                    for sample_time, value in endpoint_samples
                    if now_monotonic - sample_time <= 60.0
                ]
                _p50, source_metric.endpoint_wait_p95_ms, _max = self._percentiles(
                    recent_endpoint
                )

    def _record_source_complete_locked(
        self,
        device_id: str,
        failed: bool = False,
        timed_out: bool = False,
        wait_ms: float | None = None,
        source: str | None = None,
    ) -> None:
        source_key = source or self._device_sources.get(device_id, "unknown")
        source_metric = self._ensure_source(source_key)
        source_metric.polls_completed += 1
        if failed:
            source_metric.polls_failed += 1
        if timed_out:
            source_metric.polls_timed_out += 1
        if source_metric.active > 0:
            source_metric.active -= 1
        elif source_metric.queued > 0:
            source_metric.queued -= 1
        if wait_ms is not None:
            source_metric.total_wait_ms += max(0.0, float(wait_ms))
            if source_metric.polls_completed > 0:
                source_metric.average_wait_ms = (
                    source_metric.total_wait_ms / source_metric.polls_completed
                )

    def record_success(
        self,
        device_id: str,
        duration_ms: float,
        values_count: int,
        warning: str = "",
        wait_ms: float | None = None,
        poll_ms: float | None = None,
        publish_ms: float | None = None,
    ) -> None:
        with self._lock:
            poll = self._consume_active_poll_locked(device_id)
            if poll is None:
                return
            metric = self._ensure(device_id)
            metric.polls_succeeded += 1
            metric.total_duration_ms += max(0.0, duration_ms)
            if metric.polls_succeeded > 0:
                metric.average_duration_ms = (
                    metric.total_duration_ms / metric.polls_succeeded
                )
            if metric.min_duration_ms is None or duration_ms < metric.min_duration_ms:
                metric.min_duration_ms = duration_ms
            if metric.max_duration_ms is None or duration_ms > metric.max_duration_ms:
                metric.max_duration_ms = duration_ms
            metric.last_duration_ms = duration_ms
            metric.last_wait_ms = wait_ms
            metric.last_poll_ms = poll_ms
            metric.last_publish_ms = publish_ms
            metric.last_status = "success"
            if warning:
                metric.last_error = warning[:500]
            metric.last_success_utc = _utc_now()
            metric.last_update_utc = _utc_now()
            self._record_wait_sample_locked(wait_ms)
            self._record_device_sample_locked(
                self._device_duration_samples, device_id, duration_ms
            )
            self._record_device_sample_locked(
                self._device_wait_samples, device_id, wait_ms
            )
            self._record_source_complete_locked(
                device_id,
                wait_ms=wait_ms,
                source=poll.source,
            )

    def record_timeout(
        self,
        device_id: str,
        duration_ms: float,
        timeout_s: int,
        wait_ms: float | None = None,
        poll_ms: float | None = None,
        publish_ms: float | None = None,
    ) -> None:
        with self._lock:
            poll = self._consume_active_poll_locked(device_id)
            if poll is None:
                return
            metric = self._ensure(device_id)
            metric.polls_failed += 1
            metric.polls_timed_out += 1
            metric.last_duration_ms = duration_ms
            metric.last_wait_ms = wait_ms
            metric.last_poll_ms = poll_ms
            metric.last_publish_ms = publish_ms
            metric.last_status = "timeout"
            metric.last_error = f"Timeout after {timeout_s}s"[:500]
            metric.last_update_utc = _utc_now()
            self._record_wait_sample_locked(wait_ms)
            self._record_device_sample_locked(
                self._device_duration_samples, device_id, duration_ms
            )
            self._record_device_sample_locked(
                self._device_wait_samples, device_id, wait_ms
            )
            self._record_source_complete_locked(
                device_id,
                failed=True,
                timed_out=True,
                wait_ms=wait_ms,
                source=poll.source,
            )

    def record_failure(
        self,
        device_id: str,
        duration_ms: float,
        error: str,
        wait_ms: float | None = None,
        poll_ms: float | None = None,
        publish_ms: float | None = None,
    ) -> None:
        with self._lock:
            poll = self._consume_active_poll_locked(device_id)
            if poll is None:
                return
            metric = self._ensure(device_id)
            metric.polls_failed += 1
            metric.last_duration_ms = duration_ms
            metric.last_wait_ms = wait_ms
            metric.last_poll_ms = poll_ms
            metric.last_publish_ms = publish_ms
            metric.last_status = "error"
            metric.last_error = error[:500]
            metric.last_update_utc = _utc_now()
            self._record_wait_sample_locked(wait_ms)
            self._record_device_sample_locked(
                self._device_duration_samples, device_id, duration_ms
            )
            self._record_device_sample_locked(
                self._device_wait_samples, device_id, wait_ms
            )
            self._record_source_complete_locked(
                device_id,
                failed=True,
                wait_ms=wait_ms,
                source=poll.source,
            )

    def rename(self, old_id: str, new_id: str) -> None:
        """Move accumulated metrics from old_id to new_id, dropping old_id."""
        with self._lock:
            if old_id in self._devices and old_id != new_id:
                self._devices[new_id] = self._devices.pop(old_id)
                if old_id in self._device_sources:
                    self._device_sources[new_id] = self._device_sources.pop(old_id)
                if old_id in self._active_polls_by_device:
                    existing = self._active_polls_by_device.setdefault(new_id, deque())
                    existing.extend(self._active_polls_by_device.pop(old_id))
                if old_id in self._device_duration_samples:
                    existing_duration = self._device_duration_samples.setdefault(
                        new_id, deque(maxlen=2000)
                    )
                    existing_duration.extend(self._device_duration_samples.pop(old_id))
                if old_id in self._device_wait_samples:
                    existing_wait = self._device_wait_samples.setdefault(
                        new_id, deque(maxlen=2000)
                    )
                    existing_wait.extend(self._device_wait_samples.pop(old_id))

    def drop(self, device_id: str) -> None:
        """Remove metrics for a device that no longer exists."""
        with self._lock:
            self._devices.pop(device_id, None)
            self._last_start_monotonic.pop(device_id, None)
            self._device_sources.pop(device_id, None)
            self._discard_active_polls_locked(device_id)
            self._device_duration_samples.pop(device_id, None)
            self._device_wait_samples.pop(device_id, None)

    def clear_all(self) -> None:
        """Clear all metrics for all devices."""
        with self._lock:
            self._devices.clear()
            self._last_start_monotonic.clear()
            self._device_sources.clear()
            self._sources.clear()
            self._wait_samples.clear()
            self._source_wait_samples.clear()
            self._source_endpoint_wait_samples.clear()
            self._source_queue_started.clear()
            self._active_polls_by_device.clear()
            self._device_duration_samples.clear()
            self._device_wait_samples.clear()
            self.polls_in_flight = 0

    def prune_unknown(self, valid_ids: set[str]) -> int:
        """Drop metric rows that do not belong to currently known device identities."""
        with self._lock:
            stale_ids = [
                device_id for device_id in self._devices if device_id not in valid_ids
            ]
            for device_id in stale_ids:
                self._devices.pop(device_id, None)
                self._last_start_monotonic.pop(device_id, None)
                self._device_sources.pop(device_id, None)
                self._discard_active_polls_locked(device_id)
                self._device_duration_samples.pop(device_id, None)
                self._device_wait_samples.pop(device_id, None)
            return len(stale_ids)

    def snapshot(self) -> dict:
        with self._lock:
            now_monotonic = monotonic()
            devices: dict[str, dict[str, Any]] = {}
            for device_id, metric in self._devices.items():
                payload = asdict(metric)
                payload["duration_load_avg_ms"] = self._device_load_average_locked(
                    self._device_duration_samples, device_id, now_monotonic
                )
                payload["wait_load_avg_ms"] = self._device_load_average_locked(
                    self._device_wait_samples, device_id, now_monotonic
                )
                devices[device_id] = payload
            sources = {
                source: asdict(metric) for source, metric in self._sources.items()
            }
            polls_in_flight = self.polls_in_flight
            wait_pressure = self._wait_pressure_locked(60)
        totals = {
            "devices": len(devices),
            "polls_started": sum(
                int(item["polls_started"]) for item in devices.values()
            ),
            "polls_succeeded": sum(
                int(item["polls_succeeded"]) for item in devices.values()
            ),
            "polls_failed": sum(int(item["polls_failed"]) for item in devices.values()),
            "polls_timed_out": sum(
                int(item["polls_timed_out"]) for item in devices.values()
            ),
        }
        # Backpressure metrics
        semaphore_available = 0
        adaptive_concurrency: dict[str, Any] = {}
        if self.global_poll_semaphore is not None:
            snapshot = getattr(self.global_poll_semaphore, "snapshot", None)
            if callable(snapshot):
                adaptive_concurrency = dict(snapshot())
                semaphore_available = int(adaptive_concurrency.get("available", 0))
            else:
                semaphore_available = self.global_poll_semaphore._value
        backpressure = {
            "polls_in_flight": polls_in_flight,
            "semaphore_available": semaphore_available,
            "wait_pressure": wait_pressure,
            "adaptive_concurrency": adaptive_concurrency,
        }
        return {
            "generated_at_utc": _utc_now(),
            "totals": totals,
            "backpressure": backpressure,
            "sources": sources,
            "devices": devices,
        }
