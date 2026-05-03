from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt import pollers
from ups2mqtt.model import DeviceConfig


def _device() -> DeviceConfig:
    return DeviceConfig(
        id="snmp-device",
        source="test_snmp_driver",
        host="192.0.2.10",
        snmp_port=1161,
        snmp_community="public",
    )


def test_snmp_poll_batches_single_oid_specs(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_get_many(
        host: str,
        community: str,
        oids: list[str],
        *,
        port: int = 161,
        timeout: int = 2,
    ) -> dict[str, str]:
        assert host == "192.0.2.10"
        assert community == "public"
        assert port == 1161
        assert timeout == 2
        calls.append(oids)
        return {
            "1.3.6.1.2.1.1.1.0": "42",
            "1.3.6.1.2.1.1.2.0": "125",
        }

    monkeypatch.setattr(pollers, "_snmp_get_many_sync", fake_get_many)
    monkeypatch.setattr(
        pollers,
        "_filter_snmp_oids_by_catalog",
        lambda _device, profile: profile.get("oids", {}),
    )

    values = pollers._poll_snmp_sync(
        _device(),
        {
            "protocol": "snmp",
            "oids": {
                "answer": {"oid": "1.3.6.1.2.1.1.1.0", "poll_group": "fast"},
                "scaled": {
                    "oid": ".1.3.6.1.2.1.1.2.0",
                    "poll_group": "fast",
                    "scale": 0.1,
                },
                "missing": {"oid": "1.3.6.1.2.1.1.3.0", "poll_group": "fast"},
            },
        },
        {"fast"},
    )

    assert calls == [
        [
            "1.3.6.1.2.1.1.1.0",
            "1.3.6.1.2.1.1.2.0",
            "1.3.6.1.2.1.1.3.0",
        ]
    ]
    assert values == {"answer": 42, "scaled": 12.5}


def test_snmp_poll_preserves_multi_candidate_fallback(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_get_many(
        host: str,
        community: str,
        oids: list[str],
        *,
        port: int = 161,
        timeout: int = 2,
    ) -> dict[str, str]:
        assert host == "192.0.2.10"
        assert community == "public"
        assert port == 1161
        assert timeout == 2
        calls.append(oids)
        return {"1.3.6.1.2.1.1.2.0": "ok"}

    monkeypatch.setattr(pollers, "_snmp_get_many_sync", fake_get_many)
    monkeypatch.setattr(
        pollers,
        "_snmp_get_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("multi-candidate fallback must not use sequential GETs")
        ),
    )
    monkeypatch.setattr(
        pollers,
        "_filter_snmp_oids_by_catalog",
        lambda _device, profile: profile.get("oids", {}),
    )

    values = pollers._poll_snmp_sync(
        _device(),
        {
            "protocol": "snmp",
            "oids": {
                "name": {
                    "oids": [
                        "1.3.6.1.2.1.1.1.0",
                        "1.3.6.1.2.1.1.2.0",
                    ],
                    "poll_group": "fast",
                },
            },
        },
        {"fast"},
    )

    assert calls == [["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.2.0"]]
    assert values == {"name": "ok"}


def test_snmp_candidate_batch_preserves_declared_order(monkeypatch) -> None:
    monkeypatch.setattr(
        pollers,
        "_snmp_get_many_sync",
        lambda *_args, **_kwargs: {
            "1.3.6.1.2.1.1.1.0": "first",
            "1.3.6.1.2.1.1.2.0": "second",
        },
    )
    monkeypatch.setattr(
        pollers,
        "_filter_snmp_oids_by_catalog",
        lambda _device, profile: profile.get("oids", {}),
    )

    values = pollers._poll_snmp_sync(
        _device(),
        {
            "protocol": "snmp",
            "oids": {
                "name": {
                    "oids": [
                        "1.3.6.1.2.1.1.1.0",
                        "1.3.6.1.2.1.1.2.0",
                    ],
                    "poll_group": "fast",
                },
            },
        },
        {"fast"},
    )

    assert values == {"name": "first"}


def test_apc_snmp_metadata_refresh_batches_identity_and_probe_oids(monkeypatch) -> None:
    device = DeviceConfig(
        id="apc-snmp",
        source="ups_snmp_apc_mib",
        host="192.0.2.20",
        snmp_port=1161,
        snmp_community="public",
    )
    calls: list[list[str]] = []
    with pollers._APC_SNMP_CACHE_LOCK:
        pollers._APC_SNMP_CACHE.clear()

    def fake_get_many(
        host: str,
        community: str,
        oids: list[str],
        *,
        port: int = 161,
        timeout: int = 2,
    ) -> dict[str, str]:
        assert host == "192.0.2.20"
        assert community == "public"
        assert port == 1161
        assert timeout == 2
        calls.append(oids)
        return {
            pollers.SMARTUPS_OID_MODEL: "Smart-UPS",
            pollers.SMARTUPS_OID_SERIAL: "SN123",
            f"{pollers.UIO_SENSOR_STATUS_TEMP_C_BASE}.1.1": "255",
            pollers.SMARTUPS_OID_INPUT_FREQUENCY: "600",
        }

    monkeypatch.setattr(pollers, "_snmp_get_many_sync", fake_get_many)
    monkeypatch.setattr(
        pollers,
        "_snmp_get_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("APC metadata refresh must not use sequential GETs")
        ),
    )

    cache = pollers._maybe_refresh_apc_snmp_metadata(device)

    assert len(calls) == 1
    assert pollers.SMARTUPS_OID_MODEL in calls[0]
    assert pollers.SMARTUPS_OID_SERIAL in calls[0]
    assert f"{pollers.UIO_SENSOR_STATUS_TEMP_C_BASE}.1.1" in calls[0]
    assert cache.metadata["model"] == "Smart-UPS"
    assert cache.metadata["serial_number"] == "SN123"
    assert cache.detection["temp_1_oid"] == f"{pollers.UIO_SENSOR_STATUS_TEMP_C_BASE}.1.1"
    assert cache.detection["frequency_oid"] == pollers.SMARTUPS_OID_INPUT_FREQUENCY


def test_apc_external_probe_merge_batches_detected_oids(monkeypatch) -> None:
    device = DeviceConfig(
        id="apc-snmp",
        source="ups_snmp_apc_mib",
        host="192.0.2.20",
        snmp_port=1161,
        snmp_community="public",
    )
    calls: list[list[str]] = []

    def fake_get_many(
        host: str,
        community: str,
        oids: list[str],
        *,
        port: int = 161,
        timeout: int = 2,
    ) -> dict[str, str]:
        assert host == "192.0.2.20"
        assert community == "public"
        assert port == 1161
        assert timeout == 2
        calls.append(oids)
        return {
            pollers.SMARTUPS_OID_INPUT_FREQUENCY: "600",
            f"{pollers.UIO_SENSOR_STATUS_TEMP_C_BASE}.1.1": "255",
        }

    monkeypatch.setattr(pollers, "_snmp_get_many_sync", fake_get_many)
    monkeypatch.setattr(
        pollers,
        "_snmp_get_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("APC external probe merge must not use sequential GETs")
        ),
    )

    values: dict[str, object] = {}
    pollers._merge_apc_external_probe_data(
        device,
        values,
        {
            "frequency_oid": pollers.SMARTUPS_OID_INPUT_FREQUENCY,
            "temp_1_oid": f"{pollers.UIO_SENSOR_STATUS_TEMP_C_BASE}.1.1",
        },
    )

    assert calls == [
        [
            pollers.SMARTUPS_OID_INPUT_FREQUENCY,
            f"{pollers.UIO_SENSOR_STATUS_TEMP_C_BASE}.1.1",
        ]
    ]
    assert values["input_frequency"] == 60.0
    assert values["measure_ups_temp_probe1"] == 25.5


def test_snmp_value_text_skips_missing_oid_sentinels() -> None:
    class NoSuchObject:
        def __str__(self) -> str:
            return "No Such Object"

    assert pollers._snmp_value_text(NoSuchObject()) is None
    assert pollers._snmp_value_text(123) == "123"
