from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt import main as main_module


def test_expected_poll_failures_include_connection_errors() -> None:
    assert main_module._is_expected_poll_failure(ConnectionError("lost")) is True


def test_unexpected_poll_failures_still_use_traceback_logging() -> None:
    assert main_module._is_expected_poll_failure(RuntimeError("boom")) is False


def test_poll_failure_backoff_doubles_until_cap() -> None:
    assert main_module._poll_failure_backoff_multiplier(0) == 1
    assert main_module._poll_failure_backoff_multiplier(1) == 2
    assert main_module._poll_failure_backoff_multiplier(2) == 4
    assert main_module._poll_failure_backoff_multiplier(4) == 16
    assert main_module._poll_failure_backoff_multiplier(5) == 16
