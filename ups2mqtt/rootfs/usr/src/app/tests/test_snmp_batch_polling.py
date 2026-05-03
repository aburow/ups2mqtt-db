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
    calls: list[str] = []

    def fake_get_sync(
        host: str, community: str, oid: str, *, port: int = 161
    ) -> str | None:
        assert host == "192.0.2.10"
        assert community == "public"
        assert port == 1161
        calls.append(oid)
        return "ok" if oid == "1.3.6.1.2.1.1.2.0" else None

    monkeypatch.setattr(pollers, "_snmp_get_sync", fake_get_sync)
    monkeypatch.setattr(
        pollers,
        "_snmp_get_many_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("single-OID batch should not be used")
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

    assert calls == ["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.2.0"]
    assert values == {"name": "ok"}


def test_snmp_value_text_skips_missing_oid_sentinels() -> None:
    class NoSuchObject:
        def __str__(self) -> str:
            return "No Such Object"

    assert pollers._snmp_value_text(NoSuchObject()) is None
    assert pollers._snmp_value_text(123) == "123"
