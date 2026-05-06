from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.database import Database
from ups2mqtt.log_buffer import LogBuffer
from ups2mqtt.model import DeviceConfig, ProfileConfig
from ups2mqtt.pollers import _nut_guess_ups_name
from ups2mqtt import web as web_module
from ups2mqtt.store import DeviceStore
from ups2mqtt.web import start_web_server

_NUT_PROFILE = {
    "protocol": "nut",
    "profile_id": "nut_network_upsd",
    "source": "nut",
    "poll_groups": {
        "fast": {"interval_s": 15},
        "slow": {"interval_s": 60},
    },
    "nut": {
        "status_map": {
            "LB": {"key": "battery_low", "value": True},
            "OL": {"key": "load_on_source", "value": True},
        },
        "variables": {
            "battery.charge": {
                "key": "battery_charge",
                "poll_group": "fast",
                "type": "float",
            },
            "ups.status": {
                "key": "ups_status_raw",
                "poll_group": "fast",
                "type": "str",
            },
        },
    },
}

_APCUPSD_PROFILE = {
    "protocol": "apcupsd",
    "profile_id": "apcupsd_network_nis",
    "source": "apcupsd",
    "poll_groups": {
        "fast": {"interval_s": 15},
        "slow": {"interval_s": 60},
    },
    "apcupsd": {
        "fields": {
            "BCHARGE": {"key": "battery_charge", "poll_group": "fast", "type": "float"},
            "LINEV": {"key": "input_voltage", "poll_group": "fast", "type": "float"},
            "LOADPCT": {"key": "output_load", "poll_group": "fast", "type": "float"},
            "TIMELEFT": {
                "key": "runtime_remaining",
                "poll_group": "fast",
                "type": "float",
            },
        }
    },
}


def _fetch(base_url: str, path: str) -> tuple[int, str]:
    request = Request(f"{base_url}{path}")
    try:
        with urlopen(request) as response:  # nosec B310
            return int(response.status), response.read().decode("utf-8")
    except HTTPError as err:
        return int(err.code), err.read().decode("utf-8")


def _post(base_url: str, path: str, data: dict[str, str]) -> tuple[int, str]:
    encoded = urlencode(data).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request) as response:  # nosec B310
            return int(response.status), response.read().decode("utf-8")
    except HTTPError as err:
        return int(err.code), err.read().decode("utf-8")


def _post_with_headers(
    base_url: str,
    path: str,
    data: dict[str, str],
) -> tuple[int, str, dict[str, str]]:
    encoded = urlencode(data).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request) as response:  # nosec B310
            return (
                int(response.status),
                response.read().decode("utf-8"),
                dict(response.headers.items()),
            )
    except HTTPError as err:
        return int(err.code), err.read().decode("utf-8"), dict(err.headers.items())


def _start_test_server(
    tmp_path: Path,
    discover_nut_variables=None,
    discover_apcupsd_variables=None,
):
    db = Database(str(tmp_path / "test.db"))
    store = DeviceStore([], db)
    server = start_web_server(
        host="127.0.0.1",
        port=0,
        store=store,
        get_source_names=lambda: [
            "cyberpower_modbus_single_phase",
            "nut_network_upsd",
            "apcupsd_network_nis",
        ],
        log_buffer=LogBuffer(),
        get_capability_status=lambda: {},
        trigger_capability_reload=lambda: None,
        trigger_republish_discovery=lambda: None,
        get_metrics_snapshot=lambda: {},
        trigger_reload=lambda: None,
        get_capability_profiles=lambda: {
            "nut_network_upsd": dict(_NUT_PROFILE),
            "apcupsd_network_nis": dict(_APCUPSD_PROFILE),
        },
        discover_nut_variables=discover_nut_variables,
        discover_apcupsd_variables=discover_apcupsd_variables,
    )
    return server


def test_devices_page_renders_profile_builder_menu_item(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(base_url, "/htmx/devices")
        assert status == HTTPStatus.OK
        assert "Profile Builder" in body
        assert "@click=\"loadPanel('profile-builder')\"" in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_panel_renders_via_existing_panel_route(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(base_url, "/htmx/devices/partials/panel/profile-builder")
        assert status == HTTPStatus.OK
        assert 'id="profile-builder-panel"' in body
        assert 'hx-post="/htmx/profile-builder/actions/discover"' in body
        assert 'id="profile-builder-discovery-results"' in body
        assert 'name="connection_type" value="nut"' in body
        assert "Build reusable profiles from live NUT or APCUPSD discovery" in body
        assert "Saved profiles keep selected capability choices" in body
        assert "Used only for this discovery request." in body
        assert 'name="port"' in body
        assert 'value="3493"' in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_panel_apcupsd_defaults_port_to_3551(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(
            base_url,
            "/htmx/devices/partials/panel/profile-builder?connection_type=apcupsd",
        )
        assert status == HTTPStatus.OK
        assert 'value="3551"' in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_panel_preserves_explicit_port_from_context(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(
            base_url,
            "/htmx/devices/partials/panel/profile-builder?connection_type=apcupsd&port=4011",
        )
        assert status == HTTPStatus.OK
        assert 'value="4011"' in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_panel_switches_default_port_when_connection_type_changes(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(
            base_url,
            "/htmx/devices/partials/panel/profile-builder?current_connection_type=nut&connection_type=apcupsd&port=3493",
        )
        assert status == HTTPStatus.OK
        assert 'value="3551"' in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_hides_starttls_when_selected_reader_does_not_support_it(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(base_url, "/htmx/devices/partials/panel/profile-builder")
        assert status == HTTPStatus.OK
        assert "Use STARTTLS" not in body
        assert (
            "STARTTLS is not available with the selected NUT discovery reader." in body
        )
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_validation_errors_do_not_attempt_discovery(
    tmp_path: Path,
) -> None:
    discovery_calls: list[tuple[str, int, str, bool]] = []

    def _discover(
        host: str, port: int, ups_name: str, use_starttls: bool
    ) -> dict[str, str]:
        discovery_calls.append((host, port, ups_name, use_starttls))
        return {"ups.status": "OL"}

    server = _start_test_server(tmp_path, discover_nut_variables=_discover)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/discover",
            {"host": "", "ups_name": ""},
        )
        assert status == HTTPStatus.OK
        assert "Host is required" in body
        assert discovery_calls == []
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_missing_reader_returns_html_error_fragment(
    tmp_path: Path,
) -> None:
    def _discover(
        host: str, port: int, ups_name: str, use_starttls: bool
    ) -> dict[str, str]:
        raise FileNotFoundError("NUT reader not found")

    server = _start_test_server(tmp_path, discover_nut_variables=_discover)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/discover",
            {"host": "192.0.2.10", "ups_name": "devups", "port": "3493"},
        )
        assert status == HTTPStatus.INTERNAL_SERVER_ERROR
        assert "NUT discovery reader is unavailable in this runtime." in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_prefers_bundled_nut_reader_path() -> None:
    candidates = web_module._nutpoller_candidate_paths()
    assert len(candidates) >= 1
    assert candidates[0].name == "nutpoller.py"
    assert "ups2mqtt/vendor/nutpoller.py" in str(candidates[0])
    assert candidates[0].exists()


def test_profile_builder_mocked_nut_discovery_renders_selectable_variables_and_plain_mode(
    tmp_path: Path,
) -> None:
    discovery_calls: list[tuple[str, int, str, bool]] = []

    def _discover(
        host: str, port: int, ups_name: str, use_starttls: bool
    ) -> dict[str, str]:
        discovery_calls.append((host, port, ups_name, use_starttls))
        return {
            "battery.charge": "100",
            "ups.status": "OL",
        }

    server = _start_test_server(tmp_path, discover_nut_variables=_discover)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/discover",
            {"host": "192.0.2.10", "ups_name": "devups", "port": "3493"},
        )
        assert status == HTTPStatus.OK
        assert discovery_calls == [("192.0.2.10", 3493, "devups", False)]
        assert "Discovered NUT Capabilities" in body
        assert "<code>battery_charge</code>" in body
        assert "<code>load_on_source</code>" in body
        assert 'type="checkbox"' in body
        assert 'name="sensor_poll_group__battery_charge"' in body
        assert 'name="sensor_poll_group__load_on_source"' in body
        assert ">Fast<" in body
        assert ">Slow<" in body
        assert "Save Profile" in body
        assert "selected reusable capabilities and preferences" in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_discovery_honors_explicit_port_after_connection_type_toggle(
    tmp_path: Path,
) -> None:
    discovery_calls: list[tuple[str, int, str, bool]] = []

    def _discover(
        host: str, port: int, ups_name: str, use_starttls: bool
    ) -> dict[str, str]:
        discovery_calls.append((host, port, ups_name, use_starttls))
        return {
            "battery.charge": "100",
            "ups.status": "OL",
        }

    server = _start_test_server(tmp_path, discover_nut_variables=_discover)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body = _post(
            base_url,
            "/htmx/profile-builder/actions/discover",
            {
                "current_connection_type": "apcupsd",
                "connection_type": "nut",
                "host": "192.0.2.10",
                "ups_name": "devups",
                "port": "3551",
            },
        )
        assert status == HTTPStatus.OK
        assert discovery_calls == [("192.0.2.10", 3551, "devups", False)]
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_mocked_apcupsd_discovery_renders_selectable_fields(
    tmp_path: Path,
) -> None:
    discovery_calls: list[tuple[str, int]] = []

    def _discover(host: str, port: int) -> dict[str, str]:
        discovery_calls.append((host, port))
        return {
            "BCHARGE": "100.0 Percent",
            "LINEV": "238.0 Volts",
            "TIMELEFT": "24.0 Minutes",
            "VENDORX": "custom",
        }

    server = _start_test_server(
        tmp_path,
        discover_apcupsd_variables=_discover,
    )
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/discover",
            {
                "connection_type": "apcupsd",
                "host": "192.0.2.40",
                "port": "3551",
                "ups_name": "",
            },
        )
        assert status == HTTPStatus.OK
        assert discovery_calls == [("192.0.2.40", 3551)]
        assert "Discovered APCUPSD Capabilities" in body
        assert (
            "Generated APCUPSD profiles save against the generic reusable APCUPSD runtime contract."
            in body
        )
        assert "<code>battery_charge</code>" in body
        assert "<code>VENDORX</code>" in body
        assert "Raw Source" in body
        assert 'name="sensor_poll_group__battery_charge"' in body
        assert ">Fast<" in body
        assert ">Slow<" in body
        assert 'name="driver_key" value="apcupsd_network_nis"' in body
        assert 'name="connection_type" value="apcupsd"' in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_save_persists_reusable_nut_profile_without_connection_details(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/save",
            {
                "profile_name": "NUT Builder Profile",
                "discovered_sensor_key": "battery_charge",
                "sensor_key__battery_charge": "1",
                "sensor_mqtt__battery_charge": "1",
                "sensor_poll_group__battery_charge": "fast",
            },
        )
        assert status == HTTPStatus.OK
        assert "Saved reusable NUT profile NUT Builder Profile" in body
        assert (
            "host/IP, port, UPS name, credentials, and STARTTLS settings were not saved."
            in body
        )

        db = Database(str(tmp_path / "test.db"))
        profiles = db.load_profiles()
        saved = next(item for item in profiles if item.name == "NUT Builder Profile")
        assert saved.driver_key == "nut_network_upsd"
        assert saved.selected_sensors == ["battery_charge"]
        assert saved.sensor_preferences is not None
        assert saved.sensor_preferences["battery_charge"]["poll_group"] == "fast"
        assert "host" not in saved.config_payload
        assert "ups_name" not in saved.config_payload
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_save_persists_reusable_apcupsd_profile_without_endpoint_details(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/save",
            {
                "connection_type": "apcupsd",
                "driver_key": "apcupsd_network_nis",
                "profile_name": "APCUPSD Builder Profile",
                "discovered_sensor_key": "battery_charge",
                "sensor_key__battery_charge": "1",
                "sensor_mqtt__battery_charge": "1",
                "sensor_poll_group__battery_charge": "slow",
            },
        )
        assert status == HTTPStatus.OK
        assert "Saved reusable APCUPSD profile APCUPSD Builder Profile" in body
        assert (
            "host/IP, port, UPS name, credentials, and STARTTLS settings were not saved."
            in body
        )

        db = Database(str(tmp_path / "test.db"))
        profiles = db.load_profiles()
        saved = next(
            item for item in profiles if item.name == "APCUPSD Builder Profile"
        )
        assert saved.driver_key == "apcupsd_network_nis"
        assert saved.selected_sensors == ["battery_charge"]
        assert saved.sensor_preferences is not None
        assert saved.sensor_preferences["battery_charge"]["poll_group"] == "slow"
        assert "host" not in saved.config_payload
        assert "port" not in saved.config_payload
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_save_normalizes_raw_apcupsd_keys_to_canonical(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/save",
            {
                "connection_type": "apcupsd",
                "driver_key": "apcupsd_network_nis",
                "profile_name": "APCUPSD Raw Key Profile",
                "discovered_sensor_key": "BCHARGE",
                "sensor_key__BCHARGE": "1",
                "sensor_mqtt__BCHARGE": "1",
                "sensor_poll_group__BCHARGE": "fast",
            },
        )
        assert status == HTTPStatus.OK
        assert "Saved reusable APCUPSD profile APCUPSD Raw Key Profile" in body

        db = Database(str(tmp_path / "test.db"))
        profiles = db.load_profiles()
        saved = next(
            item for item in profiles if item.name == "APCUPSD Raw Key Profile"
        )
        assert saved.selected_sensors == ["battery_charge"]
        assert "BCHARGE" not in saved.selected_sensors
        assert saved.sensor_preferences is not None
        assert saved.sensor_preferences["battery_charge"]["poll_group"] == "fast"
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_save_apcupsd_rejects_empty_selection(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/save",
            {
                "connection_type": "apcupsd",
                "driver_key": "apcupsd_network_nis",
                "profile_name": "APCUPSD Empty Selection",
                "discovered_sensor_key": "BCHARGE",
                "sensor_key__BCHARGE": "1",
            },
        )
        assert status == HTTPStatus.OK
        assert "Select at least one APCUPSD capability" in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_discovery_includes_unknown_vendor_variables(
    tmp_path: Path,
) -> None:
    def _discover(
        host: str, port: int, ups_name: str, use_starttls: bool
    ) -> dict[str, str]:
        return {
            "battery.charge": "98",
            "vendor.mode": "eco",
            "x-apc-custom.flag": "1",
        }

    server = _start_test_server(tmp_path, discover_nut_variables=_discover)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/discover",
            {"host": "192.0.2.10", "ups_name": "devups", "port": "3493"},
        )
        assert status == HTTPStatus.OK
        assert "<code>battery_charge</code>" in body
        assert "<code>vendor.mode</code>" in body
        assert "<code>x-apc-custom.flag</code>" in body
        assert 'name="sensor_mqtt__vendor.mode"' in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_save_allows_unknown_discovered_variables(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/save",
            {
                "profile_name": "NUT Vendor Profile",
                "discovered_sensor_key": "vendor.mode",
                "sensor_key__vendor.mode": "1",
                "sensor_mqtt__vendor.mode": "1",
                "sensor_poll_group__vendor.mode": "slow",
            },
        )
        assert status == HTTPStatus.OK
        assert "Saved reusable NUT profile NUT Vendor Profile" in body

        db = Database(str(tmp_path / "test.db"))
        profiles = db.load_profiles()
        saved = next(item for item in profiles if item.name == "NUT Vendor Profile")
        assert saved.driver_key == "nut_network_upsd"
        assert saved.selected_sensors == ["vendor.mode"]
        assert saved.sensor_preferences is not None
        assert saved.sensor_preferences["vendor.mode"]["poll_group"] == "slow"
        assert "host" not in saved.config_payload
        assert "ups_name" not in saved.config_payload
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_save_omitted_poll_group_uses_default(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body = _post(
            base_url,
            "/htmx/profile-builder/actions/save",
            {
                "profile_name": "NUT Default Poll Group Profile",
                "discovered_sensor_key": "battery_charge",
                "sensor_key__battery_charge": "1",
                "sensor_mqtt__battery_charge": "1",
            },
        )
        assert status == HTTPStatus.OK
        db = Database(str(tmp_path / "test.db"))
        profiles = db.load_profiles()
        saved = next(
            item for item in profiles if item.name == "NUT Default Poll Group Profile"
        )
        assert saved.sensor_preferences is not None
        assert saved.sensor_preferences["battery_charge"]["poll_group"] == "slow"
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_save_invalid_poll_group_falls_back_to_default(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body = _post(
            base_url,
            "/htmx/profile-builder/actions/save",
            {
                "profile_name": "NUT Invalid Poll Group Profile",
                "discovered_sensor_key": "battery_charge",
                "sensor_key__battery_charge": "1",
                "sensor_mqtt__battery_charge": "1",
                "sensor_poll_group__battery_charge": "ultra-fast",
            },
        )
        assert status == HTTPStatus.OK
        db = Database(str(tmp_path / "test.db"))
        profiles = db.load_profiles()
        saved = next(
            item for item in profiles if item.name == "NUT Invalid Poll Group Profile"
        )
        assert saved.sensor_preferences is not None
        assert saved.sensor_preferences["battery_charge"]["poll_group"] == "slow"
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_discovery_ignores_malformed_empty_variable_names(
    tmp_path: Path,
) -> None:
    def _discover(
        host: str, port: int, ups_name: str, use_starttls: bool
    ) -> dict[str, str]:
        return {
            "": "bad",
            "   ": "bad2",
            "battery.charge": "100",
        }

    server = _start_test_server(tmp_path, discover_nut_variables=_discover)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profile-builder/actions/discover",
            {"host": "192.0.2.10", "ups_name": "devups", "port": "3493"},
        )
        assert status == HTTPStatus.OK
        assert "<code>battery_charge</code>" in body
        assert 'name="sensor_mqtt__battery_charge"' in body
        assert 'name="sensor_mqtt__"' not in body
    finally:
        server.shutdown()
        server.server_close()


def test_saved_nut_profile_appears_in_profiles_and_device_flow(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-profile-1",
                name="NUT Global",
                driver_key="nut_network_upsd",
                config_payload={
                    "driver_key": "nut_network_upsd",
                    "poll_groups": {"fast": 15, "slow": 60},
                    "key_precedence": {},
                },
                selected_sensors=["battery_charge"],
                sensor_preferences={
                    "battery_charge": {"mqtt_enabled": True, "poll_group": "fast"}
                },
                comments="Line one\nLine two",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, profiles_body = _fetch(
            base_url, "/htmx/devices/partials/panel/profiles"
        )
        assert status == HTTPStatus.OK
        assert "NUT Global" in profiles_body
        assert "nut_network_upsd" in profiles_body
        assert "Generated NUT profile: Line one" in profiles_body
        assert "Line two" not in profiles_body

        status, modal_body = _fetch(
            base_url,
            "/htmx/devices/partials/modal?mode=add&profile_uid=nut-profile-1&source=nut_network_upsd&host=192.0.2.50&ups_name=devups",
        )
        assert status == HTTPStatus.OK
        assert "NUT UPS Name" in modal_body
        assert 'name="ups_name"' in modal_body
        assert "Device-specific NUT identity used at poll time." in modal_body

        status, _body = _post(
            base_url,
            "/htmx/devices/actions/upsert",
            {
                "id": "nut-device-1",
                "source": "nut_network_upsd",
                "profile_uid": "nut-profile-1",
                "profile_mode": "global",
                "host": "192.0.2.50",
                "ups_name": "devups",
                "port": "3493",
                "snmp_port": "161",
                "unit_id": "1",
                "snmp_community": "public",
                "discovery_enabled": "on",
                "polling_enabled": "on",
            },
        )
        assert status == HTTPStatus.OK
        saved_device = next(
            item for item in db.load_devices() if item.id == "nut-device-1"
        )
        assert saved_device.profile_uid == "nut-profile-1"
        assert saved_device.source == "nut_network_upsd"
        assert saved_device.ups_name == "devups"
    finally:
        server.shutdown()
        server.server_close()


def test_profiles_panel_actions_target_shared_modal_content(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-profile-modal-1",
                name="Modal NUT",
                driver_key="nut_network_upsd",
                config_payload={"driver_key": "nut_network_upsd"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="modal test",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(base_url, "/htmx/devices/partials/panel/profiles")
        assert status == HTTPStatus.OK
        assert 'hx-get="/htmx/profiles/partials/form"' in body
        assert 'hx-target="#device-modal-content"' in body
        assert '@click="modalOpen = true"' in body
        assert 'hx-get="/htmx/profiles/actions/edit"' in body
        assert 'hx-get="/htmx/profiles/actions/copy"' in body
    finally:
        server.shutdown()
        server.server_close()


def test_profiles_form_posts_back_into_modal_for_validation_cycle(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(base_url, "/htmx/profiles/partials/form")
        assert status == HTTPStatus.OK
        assert 'hx-post="/htmx/profiles/actions/upsert"' in body
        assert 'hx-target="#device-modal-content"' in body
        assert 'type="button" class="btn btn-sm btn-outline-secondary" @click="closeModal()">Cancel</button>' in body
        assert 'hx-post="/htmx/profiles/actions/rediscover"' not in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_upsert_success_retargets_admin_panel_and_closes_modal(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, headers = _post_with_headers(
            base_url,
            "/htmx/profiles/actions/upsert",
            {
                "profile_uid": "",
                "profile_name": "Modal Save Profile",
                "driver_key": "nut_network_upsd",
                "comments": "saved in modal",
                "sensor_key__battery_charge": "1",
                "sensor_mqtt__battery_charge": "1",
                "sensor_poll_group__battery_charge": "slow",
            },
        )
        assert status == HTTPStatus.OK
        assert headers.get("HX-Retarget") == "#admin-panel"
        assert headers.get("HX-Reswap") == "innerHTML"
        trigger_payload = headers.get("HX-Trigger", "")
        assert "close-device-modal" in trigger_payload
    finally:
        server.shutdown()
        server.server_close()


def test_profiles_panel_comment_subtitles_and_generated_prefixes(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-subtitle-1",
                name="NUT With Comment",
                driver_key="nut_network_upsd",
                config_payload={"driver_key": "nut_network_upsd"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="\nNUT first line\nNUT second line",
                is_protected=False,
            )
        )
        db.save_profile(
            ProfileConfig(
                profile_uid="apcupsd-subtitle-1",
                name="APCUPSD With Comment",
                driver_key="apcupsd_network_nis",
                config_payload={"driver_key": "apcupsd_network_nis"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="APC line one\nAPC line two",
                is_protected=False,
            )
        )
        db.save_profile(
            ProfileConfig(
                profile_uid="other-subtitle-1",
                name="Other With Comment",
                driver_key="cyberpower_modbus_single_phase",
                config_payload={"driver_key": "cyberpower_modbus_single_phase"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="Other line one\nOther line two",
                is_protected=False,
            )
        )
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-empty-comment-1",
                name="NUT Empty Comment",
                driver_key="nut_network_upsd",
                config_payload={"driver_key": "nut_network_upsd"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(base_url, "/htmx/devices/partials/panel/profiles")
        assert status == HTTPStatus.OK
        assert "Generated NUT profile: NUT first line" in body
        assert "Generated APCUPSD profile: APC line one" in body
        assert "Other line one" in body
        assert "NUT second line" not in body
        assert "APC line two" not in body
        assert "Other line two" not in body
        assert (
            "Reusable NUT profile. Devices using it keep their own host and UPS name."
            in body
        )
    finally:
        server.shutdown()
        server.server_close()


def test_device_modal_field_visibility_by_driver_type(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="modal-nut-1",
                name="Modal NUT",
                driver_key="nut_network_upsd",
                config_payload={"driver_key": "nut_network_upsd"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="",
                is_protected=False,
            )
        )
        db.save_profile(
            ProfileConfig(
                profile_uid="modal-apcupsd-1",
                name="Modal APCUPSD",
                driver_key="apcupsd_network_nis",
                config_payload={"driver_key": "apcupsd_network_nis"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="",
                is_protected=False,
            )
        )
        db.save_profile(
            ProfileConfig(
                profile_uid="modal-snmp-1",
                name="Modal SNMP",
                driver_key="ups_snmp_apc_mib",
                config_payload={"driver_key": "ups_snmp_apc_mib"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"

        status, nut_body = _fetch(
            base_url,
            "/htmx/devices/partials/modal?mode=add&profile_uid=modal-nut-1&source=nut_network_upsd&host=192.0.2.50&ups_name=devups",
        )
        assert status == HTTPStatus.OK
        assert 'name="ups_name"' in nut_body
        assert 'name="snmp_community"' not in nut_body
        assert 'name="snmp_port"' not in nut_body
        assert 'name="unit_id"' not in nut_body

        status, apcupsd_body = _fetch(
            base_url,
            "/htmx/devices/partials/modal?mode=add&profile_uid=modal-apcupsd-1&source=apcupsd_network_nis&host=192.0.2.51&port=3551",
        )
        assert status == HTTPStatus.OK
        assert 'name="ups_name"' not in apcupsd_body
        assert 'name="snmp_community"' not in apcupsd_body
        assert 'name="snmp_port"' not in apcupsd_body
        assert 'name="unit_id"' not in apcupsd_body
        assert 'name="port"' in apcupsd_body

        status, snmp_body = _fetch(
            base_url,
            "/htmx/devices/partials/modal?mode=add&profile_uid=modal-snmp-1&source=ups_snmp_apc_mib&host=192.0.2.52",
        )
        assert status == HTTPStatus.OK
        assert 'name="snmp_community"' in snmp_body
        assert 'name="snmp_port"' in snmp_body
        assert 'name="ups_name"' not in snmp_body
    finally:
        server.shutdown()
        server.server_close()


def test_nut_ups_name_prefers_device_field_and_preserves_old_fallbacks() -> None:
    profile = {"nut": {}}
    device_with_ups_name = DeviceConfig(
        id="device-id",
        source="nut_network_upsd",
        host="192.0.2.20",
        ups_name="devups",
        name="Friendly UPS",
    )
    assert _nut_guess_ups_name(device_with_ups_name, profile) == "devups"

    device_without_ups_name = DeviceConfig(
        id="device-id",
        source="nut_network_upsd",
        host="192.0.2.20",
        name="Friendly UPS",
    )
    assert _nut_guess_ups_name(device_without_ups_name, profile) == "Friendly UPS"


def test_global_nut_profile_edit_preserves_unknown_keys(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-profile-edit-1",
                name="ION NUT Driver",
                driver_key="nut_network_upsd",
                config_payload={
                    "driver_key": "nut_network_upsd",
                    "poll_groups": {"fast": 15, "slow": 60},
                    "key_precedence": {},
                },
                selected_sensors=["battery_charge", "vendor.mode"],
                sensor_preferences={
                    "battery_charge": {"mqtt_enabled": True, "poll_group": "fast"},
                    "vendor.mode": {"mqtt_enabled": True, "poll_group": "slow"},
                },
                comments="",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, form_body = _fetch(
            base_url,
            "/htmx/profiles/actions/edit?profile_uid=nut-profile-edit-1",
        )
        assert status == HTTPStatus.OK
        assert 'name="sensor_mqtt__vendor.mode"' in form_body
        assert 'name="sensor_poll_group__vendor.mode"' in form_body

        status, _body = _post(
            base_url,
            "/htmx/profiles/actions/upsert",
            {
                "profile_uid": "nut-profile-edit-1",
                "profile_name": "ION NUT Driver",
                "driver_key": "nut_network_upsd",
                "comments": "edited",
                "sensor_key__battery_charge": "1",
                "sensor_mqtt__battery_charge": "1",
                "sensor_poll_group__battery_charge": "fast",
                "sensor_key__vendor.mode": "1",
                "sensor_mqtt__vendor.mode": "1",
                "sensor_poll_group__vendor.mode": "slow",
            },
        )
        assert status == HTTPStatus.OK
        saved = next(
            item
            for item in db.load_profiles()
            if item.profile_uid == "nut-profile-edit-1"
        )
        assert "vendor.mode" in saved.selected_sensors
        assert "host" not in saved.config_payload
        assert "ups_name" not in saved.config_payload
    finally:
        server.shutdown()
        server.server_close()


def test_profile_edit_shows_rediscover_for_editable_nut_and_apcupsd(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-rediscover-1",
                name="NUT Rediscover",
                driver_key="nut_network_upsd",
                config_payload={"driver_key": "nut_network_upsd"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="",
                is_protected=False,
            )
        )
        db.save_profile(
            ProfileConfig(
                profile_uid="apcupsd-rediscover-1",
                name="APCUPSD Rediscover",
                driver_key="apcupsd_network_nis",
                config_payload={"driver_key": "apcupsd_network_nis"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"

        status, nut_body = _fetch(
            base_url,
            "/htmx/profiles/actions/edit?profile_uid=nut-rediscover-1",
        )
        assert status == HTTPStatus.OK
        assert 'hx-post="/htmx/profiles/actions/rediscover"' in nut_body
        assert 'name="rediscover_ups_name"' in nut_body

        status, apcupsd_body = _fetch(
            base_url,
            "/htmx/profiles/actions/edit?profile_uid=apcupsd-rediscover-1",
        )
        assert status == HTTPStatus.OK
        assert 'hx-post="/htmx/profiles/actions/rediscover"' in apcupsd_body
        assert 'name="rediscover_ups_name"' not in apcupsd_body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_edit_rediscover_disabled_for_protected_profile(tmp_path: Path) -> None:
    server = _start_test_server(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-protected-rediscover-1",
                name="Protected NUT",
                driver_key="nut_network_upsd",
                config_payload={"driver_key": "nut_network_upsd"},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True}},
                comments="",
                is_protected=True,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _fetch(
            base_url,
            "/htmx/profiles/actions/edit?profile_uid=nut-protected-rediscover-1",
        )
        assert status == HTTPStatus.OK
        assert 'hx-post="/htmx/profiles/actions/rediscover"' in body
        assert "disabled" in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_rediscover_nut_merges_fields_and_preserves_preferences(
    tmp_path: Path,
) -> None:
    def _discover_nut(
        host: str, port: int, ups_name: str, use_starttls: bool
    ) -> dict[str, str]:
        assert host == "192.0.2.50"
        assert port == 3493
        assert ups_name == "apc_pdu1"
        assert use_starttls is False
        return {
            "input.current": "1.80",
            "outlet.count": "0",
            "vendor.mode": "auto",
        }

    server = _start_test_server(tmp_path, discover_nut_variables=_discover_nut)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-rediscover-merge-1",
                name="NUT Merge",
                driver_key="nut_network_upsd",
                config_payload={
                    "driver_key": "nut_network_upsd",
                    "poll_groups": {"fast": 15, "slow": 60},
                    "key_precedence": {},
                },
                selected_sensors=["vendor.mode"],
                sensor_preferences={
                    "vendor.mode": {"mqtt_enabled": True, "poll_group": "fast"}
                },
                comments="",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profiles/actions/rediscover",
            {
                "profile_uid": "nut-rediscover-merge-1",
                "profile_name": "NUT Merge",
                "driver_key": "nut_network_upsd",
                "comments": "",
                "rediscover_host": "192.0.2.50",
                "rediscover_port": "3493",
                "rediscover_ups_name": "apc_pdu1",
                "sensor_key__vendor.mode": "1",
                "sensor_mqtt__vendor.mode": "1",
                "sensor_poll_group__vendor.mode": "fast",
            },
        )
        assert status == HTTPStatus.OK
        assert 'name="sensor_key__input.current"' in body
        assert 'name="sensor_key__outlet.count"' in body
        assert 'name="sensor_mqtt__vendor.mode"' in body
        assert 'name="sensor_mqtt__input.current"' in body
        assert 'name="sensor_mqtt__outlet.count"' in body
        assert "checked" in body

        saved = next(
            item for item in db.load_profiles() if item.profile_uid == "nut-rediscover-merge-1"
        )
        assert "rediscover_host" not in saved.config_payload
        assert "rediscover_port" not in saved.config_payload
        assert "rediscover_ups_name" not in saved.config_payload
    finally:
        server.shutdown()
        server.server_close()


def test_profile_rediscover_apcupsd_merges_fields_and_preserves_preferences(
    tmp_path: Path,
) -> None:
    def _discover_apcupsd(host: str, port: int) -> dict[str, str]:
        assert host == "192.0.2.60"
        assert port == 3551
        return {
            "BCHARGE": "100.0 Percent",
            "TIMELEFT": "24.0 Minutes",
            "VENDORX": "custom",
        }

    server = _start_test_server(tmp_path, discover_apcupsd_variables=_discover_apcupsd)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="apcupsd-rediscover-merge-1",
                name="APCUPSD Merge",
                driver_key="apcupsd_network_nis",
                config_payload={
                    "driver_key": "apcupsd_network_nis",
                    "poll_groups": {"fast": 15, "slow": 60},
                    "key_precedence": {},
                },
                selected_sensors=["battery_charge"],
                sensor_preferences={
                    "battery_charge": {"mqtt_enabled": True, "poll_group": "fast"}
                },
                comments="",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body = _post(
            base_url,
            "/htmx/profiles/actions/rediscover",
            {
                "profile_uid": "apcupsd-rediscover-merge-1",
                "profile_name": "APCUPSD Merge",
                "driver_key": "apcupsd_network_nis",
                "comments": "",
                "rediscover_host": "192.0.2.60",
                "rediscover_port": "3551",
                "sensor_key__battery_charge": "1",
                "sensor_mqtt__battery_charge": "1",
                "sensor_poll_group__battery_charge": "fast",
            },
        )
        assert status == HTTPStatus.OK
        assert 'name="sensor_key__runtime_remaining"' in body
        assert 'name="sensor_key__VENDORX"' in body

        saved = next(
            item
            for item in db.load_profiles()
            if item.profile_uid == "apcupsd-rediscover-merge-1"
        )
        assert "rediscover_host" not in saved.config_payload
        assert "rediscover_port" not in saved.config_payload
    finally:
        server.shutdown()
        server.server_close()

def test_local_nut_profile_edit_is_device_only_and_preserves_unknown_keys(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-global-1",
                name="ION NUT Driver",
                driver_key="nut_network_upsd",
                config_payload={
                    "driver_key": "nut_network_upsd",
                    "poll_groups": {"fast": 15, "slow": 60},
                    "key_precedence": {},
                },
                selected_sensors=["battery_charge"],
                sensor_preferences={
                    "battery_charge": {"mqtt_enabled": True, "poll_group": "fast"}
                },
                comments="global",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body = _post(
            base_url,
            "/htmx/devices/actions/upsert",
            {
                "id": "nut-device-local-1",
                "source": "nut_network_upsd",
                "profile_uid": "nut-global-1",
                "profile_mode": "local",
                "host": "192.0.2.50",
                "ups_name": "devups",
                "port": "3493",
                "snmp_port": "161",
                "unit_id": "1",
                "snmp_community": "public",
                "discovery_enabled": "on",
                "polling_enabled": "on",
                "sensor_key__battery_charge": "1",
                "sensor_mqtt__battery_charge": "1",
                "sensor_poll_group__battery_charge": "fast",
                "sensor_key__vendor.mode": "1",
                "sensor_mqtt__vendor.mode": "1",
                "sensor_poll_group__vendor.mode": "slow",
            },
        )
        assert status == HTTPStatus.OK

        saved_device = next(
            item for item in db.load_devices() if item.id == "nut-device-local-1"
        )
        assert saved_device.profile_mode == "local"
        assert saved_device.local_selected_sensors is not None
        assert "vendor.mode" in saved_device.local_selected_sensors
        assert saved_device.local_sensor_preferences is not None
        assert "vendor.mode" in saved_device.local_sensor_preferences
        assert (
            saved_device.local_sensor_preferences["battery_charge"]["poll_group"]
            == "fast"
        )
        assert (
            saved_device.local_sensor_preferences["vendor.mode"]["poll_group"] == "slow"
        )

        global_profile = next(
            item for item in db.load_profiles() if item.profile_uid == "nut-global-1"
        )
        assert global_profile.selected_sensors == ["battery_charge"]
        assert "vendor.mode" not in global_profile.selected_sensors
        assert global_profile.sensor_preferences is not None
        assert (
            global_profile.sensor_preferences["battery_charge"]["poll_group"] == "fast"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_device_global_modal_reflects_edited_nut_profile_unknown_keys(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-global-modal-1",
                name="ION NUT Driver",
                driver_key="nut_network_upsd",
                config_payload={
                    "driver_key": "nut_network_upsd",
                    "poll_groups": {"fast": 15, "slow": 60},
                    "key_precedence": {},
                },
                selected_sensors=["battery_charge", "vendor.mode"],
                sensor_preferences={
                    "battery_charge": {"mqtt_enabled": True, "poll_group": "fast"},
                    "vendor.mode": {"mqtt_enabled": True, "poll_group": "slow"},
                },
                comments="",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, modal_body = _fetch(
            base_url,
            "/htmx/devices/partials/modal?mode=add&profile_uid=nut-global-modal-1&source=nut_network_upsd&profile_mode=global&host=192.0.2.50&ups_name=devups",
        )
        assert status == HTTPStatus.OK
        assert 'name="sensor_mqtt__vendor.mode"' in modal_body
        assert 'name="sensor_poll_group__vendor.mode"' in modal_body
    finally:
        server.shutdown()
        server.server_close()


def test_device_local_mode_initializes_from_current_global_profile_when_no_local_override(
    tmp_path: Path,
) -> None:
    server = _start_test_server(tmp_path)
    try:
        db = Database(str(tmp_path / "test.db"))
        db.save_profile(
            ProfileConfig(
                profile_uid="nut-global-init-1",
                name="ION NUT Driver",
                driver_key="nut_network_upsd",
                config_payload={
                    "driver_key": "nut_network_upsd",
                    "poll_groups": {"fast": 15, "slow": 60},
                    "key_precedence": {},
                },
                selected_sensors=["battery_charge", "vendor.mode"],
                sensor_preferences={
                    "battery_charge": {"mqtt_enabled": True, "poll_group": "fast"},
                    "vendor.mode": {"mqtt_enabled": True, "poll_group": "slow"},
                },
                comments="",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, modal_body = _fetch(
            base_url,
            "/htmx/devices/partials/modal?mode=add&profile_uid=nut-global-init-1&source=nut_network_upsd&profile_mode=local&host=192.0.2.50&ups_name=devups",
        )
        assert status == HTTPStatus.OK
        assert 'name="sensor_mqtt__vendor.mode"' in modal_body
        assert 'name="sensor_poll_group__vendor.mode"' in modal_body
        assert "Local mode: MQTT publish can be customized per device." in modal_body
    finally:
        server.shutdown()
        server.server_close()
