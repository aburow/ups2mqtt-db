from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.metrics import MetricsStore


def test_successful_poll_accounting_remains_balanced() -> None:
    metrics = MetricsStore()

    metrics.record_start("device-1", "source-a")
    metrics.record_dequeue("device-1", "source-a", wait_ms=2.0)
    metrics.record_success("device-1", 10.0, 3, wait_ms=2.0)

    snapshot = metrics.snapshot()

    assert snapshot["totals"]["polls_started"] == 1
    assert snapshot["totals"]["polls_succeeded"] == 1
    assert snapshot["backpressure"]["polls_in_flight"] == 0
    assert snapshot["sources"]["source-a"]["polls_dequeued"] == 1
    assert snapshot["sources"]["source-a"]["polls_completed"] == 1
    assert snapshot["sources"]["source-a"]["active"] == 0
    assert snapshot["sources"]["source-a"]["queued"] == 0
    assert metrics.source_totals()["source-a"] == {
        "polls_completed": 1,
        "polls_failed": 0,
        "polls_timed_out": 0,
    }


def test_stale_completion_after_clear_does_not_create_impossible_totals() -> None:
    metrics = MetricsStore()
    metrics.record_start("device-1", "source-a")

    metrics.clear_all()
    metrics.record_dequeue("device-1", "source-a", wait_ms=2.0)
    metrics.record_success("device-1", 10.0, 1)

    snapshot = metrics.snapshot()

    assert snapshot["totals"]["polls_started"] == 0
    assert snapshot["totals"]["polls_succeeded"] == 0
    assert snapshot["backpressure"]["polls_in_flight"] == 0
    assert snapshot["sources"] == {}
    assert snapshot["devices"] == {}


def test_terminal_event_without_start_is_ignored() -> None:
    metrics = MetricsStore()

    metrics.record_failure("device-1", 10.0, "boom")
    metrics.record_timeout("device-1", 10.0, 30)

    snapshot = metrics.snapshot()

    assert snapshot["totals"]["polls_started"] == 0
    assert snapshot["totals"]["polls_failed"] == 0
    assert snapshot["totals"]["polls_timed_out"] == 0
    assert snapshot["backpressure"]["polls_in_flight"] == 0
    assert snapshot["devices"] == {}


def test_drop_discards_active_poll_accounting() -> None:
    metrics = MetricsStore()
    metrics.record_start("device-1", "source-a")
    metrics.record_dequeue("device-1", "source-a", wait_ms=2.0)

    metrics.drop("device-1")
    metrics.record_success("device-1", 10.0, 1)

    snapshot = metrics.snapshot()

    assert snapshot["totals"]["polls_started"] == 0
    assert snapshot["totals"]["polls_succeeded"] == 0
    assert snapshot["backpressure"]["polls_in_flight"] == 0
    assert snapshot["sources"]["source-a"]["active"] == 0
    assert snapshot["sources"]["source-a"]["queued"] == 0
