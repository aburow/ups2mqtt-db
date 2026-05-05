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


def _start_test_server(
    tmp_path: Path,
    discover_nut_variables=None,
):
    db = Database(str(tmp_path / "test.db"))
    store = DeviceStore([], db)
    server = start_web_server(
        host="127.0.0.1",
        port=0,
        store=store,
        get_source_names=lambda: ["cyberpower_modbus_single_phase", "nut_network_upsd"],
        log_buffer=LogBuffer(),
        get_capability_status=lambda: {},
        trigger_capability_reload=lambda: None,
        trigger_republish_discovery=lambda: None,
        get_metrics_snapshot=lambda: {},
        trigger_reload=lambda: None,
        get_capability_profiles=lambda: {"nut_network_upsd": dict(_NUT_PROFILE)},
        discover_nut_variables=discover_nut_variables,
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
        assert "APCUPSD discovery is deferred" in body
        assert "Saved profiles keep selected capability choices" in body
        assert "Used only for this discovery request." in body
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
        assert "STARTTLS is not available with the selected NUT discovery reader." in body
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_validation_errors_do_not_attempt_discovery(tmp_path: Path) -> None:
    discovery_calls: list[tuple[str, int, str, bool]] = []

    def _discover(host: str, port: int, ups_name: str, use_starttls: bool) -> dict[str, str]:
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
        assert status == HTTPStatus.BAD_REQUEST
        assert "Host is required" in body
        assert discovery_calls == []
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_missing_reader_returns_html_error_fragment(tmp_path: Path) -> None:
    def _discover(host: str, port: int, ups_name: str, use_starttls: bool) -> dict[str, str]:
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

    def _discover(host: str, port: int, ups_name: str, use_starttls: bool) -> dict[str, str]:
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
        assert "Save Profile" in body
        assert "generic reusable NUT runtime contract" in body
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
            },
        )
        assert status == HTTPStatus.OK
        assert "Saved reusable NUT profile NUT Builder Profile" in body
        assert "Discovery host/IP, UPS name, credentials, and STARTTLS settings were not saved." in body

        db = Database(str(tmp_path / "test.db"))
        profiles = db.load_profiles()
        saved = next(item for item in profiles if item.name == "NUT Builder Profile")
        assert saved.driver_key == "nut_network_upsd"
        assert saved.selected_sensors == ["battery_charge"]
        assert "host" not in saved.config_payload
        assert "ups_name" not in saved.config_payload
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_discovery_includes_unknown_vendor_variables(tmp_path: Path) -> None:
    def _discover(host: str, port: int, ups_name: str, use_starttls: bool) -> dict[str, str]:
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


def test_profile_builder_save_allows_unknown_discovered_variables(tmp_path: Path) -> None:
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
            },
        )
        assert status == HTTPStatus.OK
        assert "Saved reusable NUT profile NUT Vendor Profile" in body

        db = Database(str(tmp_path / "test.db"))
        profiles = db.load_profiles()
        saved = next(item for item in profiles if item.name == "NUT Vendor Profile")
        assert saved.driver_key == "nut_network_upsd"
        assert saved.selected_sensors == ["vendor.mode"]
        assert "host" not in saved.config_payload
        assert "ups_name" not in saved.config_payload
    finally:
        server.shutdown()
        server.server_close()


def test_profile_builder_discovery_ignores_malformed_empty_variable_names(
    tmp_path: Path,
) -> None:
    def _discover(host: str, port: int, ups_name: str, use_starttls: bool) -> dict[str, str]:
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
                config_payload={"driver_key": "nut_network_upsd", "poll_groups": {"fast": 15, "slow": 60}, "key_precedence": {}},
                selected_sensors=["battery_charge"],
                sensor_preferences={"battery_charge": {"mqtt_enabled": True, "poll_group": "fast"}},
                comments="",
                is_protected=False,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, profiles_body = _fetch(base_url, "/htmx/devices/partials/panel/profiles")
        assert status == HTTPStatus.OK
        assert "NUT Global" in profiles_body
        assert "nut_network_upsd" in profiles_body
        assert "Reusable NUT profile. Devices using it keep their own host and UPS name." in profiles_body

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
        saved_device = next(item for item in db.load_devices() if item.id == "nut-device-1")
        assert saved_device.profile_uid == "nut-profile-1"
        assert saved_device.source == "nut_network_upsd"
        assert saved_device.ups_name == "devups"
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
        assert 'name="sensor__vendor.mode"' in form_body

        status, _body = _post(
            base_url,
            "/htmx/profiles/actions/upsert",
            {
                "profile_uid": "nut-profile-edit-1",
                "profile_name": "ION NUT Driver",
                "driver_key": "nut_network_upsd",
                "comments": "edited",
                "sensor__battery_charge": "on",
                "sensor__vendor.mode": "on",
            },
        )
        assert status == HTTPStatus.OK
        saved = next(item for item in db.load_profiles() if item.profile_uid == "nut-profile-edit-1")
        assert "vendor.mode" in saved.selected_sensors
        assert "host" not in saved.config_payload
        assert "ups_name" not in saved.config_payload
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
                sensor_preferences={"battery_charge": {"mqtt_enabled": True, "poll_group": "fast"}},
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
                "sensor_key__vendor.mode": "1",
                "sensor_mqtt__vendor.mode": "1",
            },
        )
        assert status == HTTPStatus.OK

        saved_device = next(item for item in db.load_devices() if item.id == "nut-device-local-1")
        assert saved_device.profile_mode == "local"
        assert saved_device.local_selected_sensors is not None
        assert "vendor.mode" in saved_device.local_selected_sensors
        assert saved_device.local_sensor_preferences is not None
        assert "vendor.mode" in saved_device.local_sensor_preferences

        global_profile = next(item for item in db.load_profiles() if item.profile_uid == "nut-global-1")
        assert global_profile.selected_sensors == ["battery_charge"]
        assert "vendor.mode" not in global_profile.selected_sensors
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
        assert 'name="sensor__vendor.mode"' in modal_body
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
        assert 'name="sensor__vendor.mode"' in modal_body
        assert "Local mode: MQTT publish can be customized per device." in modal_body
    finally:
        server.shutdown()
        server.server_close()
