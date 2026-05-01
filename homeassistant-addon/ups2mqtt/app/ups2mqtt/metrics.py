# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import monotonic

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


class MetricsStore:
    def __init__(
        self,
        global_poll_semaphore: asyncio.Semaphore | None = None,
        db: Database | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, DeviceMetrics] = {}
        self._last_start_monotonic: dict[str, float] = {}
        self.polls_in_flight = 0
        self.global_poll_semaphore = global_poll_semaphore
        self._db = db

    def _ensure(self, device_id: str) -> DeviceMetrics:
        if device_id not in self._devices:
            self._devices[device_id] = DeviceMetrics()
        return self._devices[device_id]

    def record_start(self, device_id: str) -> None:
        now_monotonic = monotonic()
        with self._lock:
            metric = self._ensure(device_id)
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
            self.polls_in_flight -= 1

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
            self.polls_in_flight -= 1

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
            metric = self._ensure(device_id)
            metric.polls_failed += 1
            metric.last_duration_ms = duration_ms
            metric.last_wait_ms = wait_ms
            metric.last_poll_ms = poll_ms
            metric.last_publish_ms = publish_ms
            metric.last_status = "error"
            metric.last_error = error[:500]
            metric.last_update_utc = _utc_now()
            self.polls_in_flight -= 1

    def rename(self, old_id: str, new_id: str) -> None:
        """Move accumulated metrics from old_id to new_id, dropping old_id."""
        with self._lock:
            if old_id in self._devices and old_id != new_id:
                self._devices[new_id] = self._devices.pop(old_id)

    def drop(self, device_id: str) -> None:
        """Remove metrics for a device that no longer exists."""
        with self._lock:
            self._devices.pop(device_id, None)
            self._last_start_monotonic.pop(device_id, None)

    def clear_all(self) -> None:
        """Clear all metrics for all devices."""
        with self._lock:
            self._devices.clear()
            self._last_start_monotonic.clear()

    def prune_unknown(self, valid_ids: set[str]) -> int:
        """Drop metric rows that do not belong to currently known device identities."""
        with self._lock:
            stale_ids = [
                device_id for device_id in self._devices if device_id not in valid_ids
            ]
            for device_id in stale_ids:
                self._devices.pop(device_id, None)
                self._last_start_monotonic.pop(device_id, None)
            return len(stale_ids)

    def snapshot(self) -> dict:
        with self._lock:
            devices = {
                device_id: asdict(metric) for device_id, metric in self._devices.items()
            }
            polls_in_flight = self.polls_in_flight
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
        if self.global_poll_semaphore is not None:
            semaphore_available = self.global_poll_semaphore._value
        backpressure = {
            "polls_in_flight": polls_in_flight,
            "semaphore_available": semaphore_available,
        }
        return {
            "generated_at_utc": _utc_now(),
            "totals": totals,
            "backpressure": backpressure,
            "devices": devices,
        }
