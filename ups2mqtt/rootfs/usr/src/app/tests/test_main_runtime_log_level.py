from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt import main as main_module


def test_runtime_log_level_defaults_to_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UPS2MQTT_LOG_LEVEL", raising=False)
    assert main_module._resolve_runtime_log_level() == main_module.logging.ERROR


def test_runtime_log_level_respects_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UPS2MQTT_LOG_LEVEL", "debug")
    assert main_module._resolve_runtime_log_level() == main_module.logging.DEBUG


def test_runtime_log_level_invalid_value_falls_back_to_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UPS2MQTT_LOG_LEVEL", "not-a-level")
    assert main_module._resolve_runtime_log_level() == main_module.logging.ERROR
