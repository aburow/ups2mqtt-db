from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.concurrency import AdjustableConcurrencyLimiter
from ups2mqtt.main import _device_poll_slot_offsets
from ups2mqtt.metrics import MetricsStore
from ups2mqtt.model import DeviceConfig


def test_try_acquire_does_not_queue_when_capacity_is_unavailable() -> None:
    async def _run() -> None:
        limiter = AdjustableConcurrencyLimiter(1)

        assert limiter.try_acquire("ups_snmp_apc_mib")
        assert not limiter.try_acquire("ups_snmp_apc_mib")

        snapshot = limiter.snapshot()
        assert snapshot["in_flight"] == 1
        assert snapshot["queued"] == 0

        await limiter.release("ups_snmp_apc_mib")
        assert limiter.try_acquire("ups_snmp_apc_mib")
        await limiter.release("ups_snmp_apc_mib")

    asyncio.run(_run())


def test_missed_capacity_metrics_do_not_create_active_poll_backlog() -> None:
    metrics = MetricsStore()

    metrics.record_missed_capacity("device-a", "ups_snmp_apc_mib")
    metrics.record_missed_capacity("device-a", "ups_snmp_apc_mib")
    metrics.record_missed_overlap("device-a", "ups_snmp_apc_mib")

    snapshot = metrics.snapshot()
    device = snapshot["devices"]["device-a"]
    source = snapshot["sources"]["ups_snmp_apc_mib"]

    assert snapshot["backpressure"]["polls_in_flight"] == 0
    assert snapshot["totals"]["polls_started"] == 0
    assert snapshot["totals"]["missed_capacity_count"] == 2
    assert snapshot["totals"]["missed_overlap_count"] == 1
    assert device["missed_capacity_count"] == 2
    assert device["missed_overlap_count"] == 1
    assert source["missed_capacity_count"] == 2
    assert source["missed_overlap_count"] == 1


def test_metrics_expose_scheduler_comparison_fields() -> None:
    metrics = MetricsStore()
    metrics.record_event_loop_lag(12.5)

    snapshot = metrics.snapshot()
    backpressure = snapshot["backpressure"]

    assert "polls_started_per_second" in backpressure
    assert "polls_completed_per_second" in backpressure
    assert "timeout_rate" in backpressure
    assert backpressure["event_loop_lag_ms"] == 12.5


def test_metrics_record_success_tracks_last_values_count() -> None:
    metrics = MetricsStore()

    metrics.record_start("device-a", "ups_snmp_apc_mib")
    metrics.record_success("device-a", duration_ms=10.0, values_count=37)

    snapshot = metrics.snapshot()

    assert snapshot["devices"]["device-a"]["last_values_count"] == 37


def test_metrics_clear_all_last_errors_only_clears_error_text() -> None:
    metrics = MetricsStore()

    metrics.record_missed_capacity("device-a", "ups_snmp_apc_mib")
    metrics.record_missed_overlap("device-b", "ups_snmp_apc_mib")

    assert metrics.clear_all_last_errors() == 2

    snapshot = metrics.snapshot()
    assert snapshot["totals"]["missed_capacity_count"] == 1
    assert snapshot["totals"]["missed_overlap_count"] == 1
    assert snapshot["devices"]["device-a"]["last_error"] == ""
    assert snapshot["devices"]["device-b"]["last_error"] == ""


def test_device_poll_slot_offsets_spread_banks_across_interval() -> None:
    devices = [
        DeviceConfig(
            id=f"device-{index:02d}",
            source="ups_snmp_apc_mib",
            host="127.0.0.1",
            device_uid=f"uid-{index:02d}",
        )
        for index in range(1, 26)
    ]

    offsets = _device_poll_slot_offsets(
        devices,
        interval_seconds=10,
        bank_size=10,
    )

    assert offsets["uid-01"] == 0.0
    assert offsets["uid-10"] == 0.0
    assert offsets["uid-11"] == 10 / 3
    assert offsets["uid-20"] == 10 / 3
    assert offsets["uid-21"] == 20 / 3
    assert offsets["uid-25"] == 20 / 3
