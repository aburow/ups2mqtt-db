from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.database import Database
from ups2mqtt.log_buffer import LogBuffer
from ups2mqtt.model import DeviceConfig, ProfileConfig
from ups2mqtt.store import DeviceStore
from ups2mqtt.versions import APP_VERSION, BACKUP_SCHEMA_NAME, BACKUP_SCHEMA_VERSION
from ups2mqtt.web import start_web_server


def _fetch(base_url: str, path: str) -> tuple[int, str, dict[str, str]]:
    request = Request(f"{base_url}{path}")
    try:
        with urlopen(request) as response:  # nosec B310
            return (
                int(response.status),
                response.read().decode("utf-8"),
                dict(response.headers.items()),
            )
    except HTTPError as err:
        return int(err.code), err.read().decode("utf-8"), dict(err.headers.items())


def _post(base_url: str, path: str, data: dict[str, str]) -> tuple[int, str, dict[str, str]]:
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


def _capability_profiles() -> dict[str, dict]:
    return {
        "apc_modbus_smt": {
            "protocol": "modbus",
            "poll_groups": {"slow": {"interval_s": 60}},
            "registers": [],
        },
        "cyberpower_modbus_single_phase": {
            "protocol": "modbus",
            "poll_groups": {"slow": {"interval_s": 60}},
            "registers": [],
        },
    }


def _start_test_server(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = Database(str(tmp_path / "test.db"))
    store = DeviceStore(db.load_devices(), db)
    server = start_web_server(
        host="127.0.0.1",
        port=0,
        store=store,
        get_source_names=lambda: sorted(_capability_profiles().keys()),
        log_buffer=LogBuffer(),
        get_capability_status=lambda: {},
        trigger_capability_reload=lambda: None,
        trigger_republish_discovery=lambda: None,
        get_metrics_snapshot=lambda: {},
        trigger_reload=lambda: None,
        get_capability_profiles=_capability_profiles,
    )
    return server, db, store


def _base_export_payload() -> dict:
    return {
        "schema": BACKUP_SCHEMA_NAME,
        "version": BACKUP_SCHEMA_VERSION,
        "exported_at": "2026-04-27T00:00:00Z",
        "devices": [],
        "profiles": [],
    }


def test_export_global_profile_device_includes_uid_and_profile_snapshot(tmp_path: Path) -> None:
    server, db, store = _start_test_server(tmp_path)
    try:
        profile = ProfileConfig(
            profile_uid="profile-1",
            name="Global Profile A",
            driver_key="apc_modbus_smt",
            config_payload={"driver_key": "apc_modbus_smt", "poll_groups": {"slow": 60}},
            selected_sensors=["runtime_remaining"],
            sensor_preferences={"runtime_remaining": {"mqtt_enabled": True, "poll_group": "slow"}},
            comments="test",
            is_protected=False,
        )
        db.save_profile(profile)
        store.upsert(
            DeviceConfig(
                id="dev-a",
                source="apc_modbus_smt",
                host="10.0.0.10",
                device_uid="device-1",
                profile_uid="profile-1",
                profile_mode="global",
                polling_enabled=True,
                discovery_enabled=True,
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body, headers = _fetch(base_url, "/htmx/maintenance/backup/export")
        assert status == HTTPStatus.OK
        assert "ups2mqtt-backup-" in headers.get("Content-Disposition", "")
        payload = json.loads(body)
        assert payload["schema"] == BACKUP_SCHEMA_NAME
        assert payload["version"] == BACKUP_SCHEMA_VERSION
        assert payload["exported_by"] == f"ups2mqtt {APP_VERSION}"
        assert payload["devices"][0]["device_uid"] == "device-1"
        assert payload["devices"][0]["profile_uid"] == "profile-1"
        assert payload["devices"][0]["profile_mode"] == "global"
        assert payload["devices"][0]["location"] == ""
        assert any(item["profile_uid"] == "profile-1" for item in payload["profiles"])
    finally:
        server.shutdown()
        server.server_close()


def test_import_global_profile_into_empty_db_preserves_uids_and_binding(tmp_path: Path) -> None:
    server, db, _store = _start_test_server(tmp_path)
    try:
        payload = _base_export_payload()
        payload["profiles"] = [
            {
                "profile_uid": "profile-1",
                "name": "Global Profile A",
                "driver_key": "apc_modbus_smt",
                "config_payload": {"driver_key": "apc_modbus_smt", "poll_groups": {"slow": 60}},
                "selected_sensors": ["runtime_remaining"],
                "sensor_preferences": {"runtime_remaining": {"mqtt_enabled": True, "poll_group": "slow"}},
                "comments": "",
                "is_protected": False,
            }
        ]
        payload["devices"] = [
            {
                "device_uid": "device-1",
                "name": "UPS A",
                "driver_key": "apc_modbus_smt",
                "profile_mode": "global",
                "profile_uid": "profile-1",
                "profile_name": "Global Profile A",
                "config": {
                    "id": "dev-a",
                    "host": "10.0.0.10",
                    "port": 502,
                    "unit_id": 1,
                    "snmp_community": "public",
                    "poll_interval": None,
                    "debug_logging": False,
                    "keep_connection_open": False,
                    "discovery_enabled": True,
                    "polling_enabled": True,
                },
                "local_profile_payload": None,
                "local_selected_sensors": None,
                "local_sensor_preferences": None,
            }
        ]
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, _headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(payload)},
        )
        assert status == HTTPStatus.OK
        devices = db.load_devices()
        profiles = db.load_profiles()
        assert any(item.device_uid == "device-1" and item.profile_uid == "profile-1" for item in devices)
        assert any(item.profile_uid == "profile-1" for item in profiles)
    finally:
        server.shutdown()
        server.server_close()


def test_import_reuses_existing_profile_when_uid_missing_and_name_driver_match(tmp_path: Path) -> None:
    server, db, _store = _start_test_server(tmp_path)
    try:
        db.save_profile(
            ProfileConfig(
                profile_uid="existing-profile",
                name="Global Profile A",
                driver_key="apc_modbus_smt",
                config_payload={"driver_key": "apc_modbus_smt", "poll_groups": {"slow": 60}},
                selected_sensors=["runtime_remaining"],
                sensor_preferences={"runtime_remaining": {"mqtt_enabled": True, "poll_group": "slow"}},
                comments="",
                is_protected=False,
            )
        )
        payload = _base_export_payload()
        payload["devices"] = [
            {
                "device_uid": "device-1",
                "name": "UPS A",
                "driver_key": "apc_modbus_smt",
                "profile_mode": "global",
                "profile_uid": None,
                "profile_name": "Global Profile A",
                "config": {
                    "id": "dev-a",
                    "host": "10.0.0.10",
                    "port": 502,
                    "unit_id": 1,
                    "snmp_community": "public",
                    "poll_interval": None,
                    "debug_logging": False,
                    "keep_connection_open": False,
                    "discovery_enabled": True,
                    "polling_enabled": True,
                },
                "local_profile_payload": None,
                "local_selected_sensors": None,
                "local_sensor_preferences": None,
            }
        ]
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, _headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(payload)},
        )
        assert status == HTTPStatus.OK
        imported = next(item for item in db.load_devices() if item.device_uid == "device-1")
        assert imported.profile_uid == "existing-profile"
    finally:
        server.shutdown()
        server.server_close()


def test_import_conflicting_profile_uid_does_not_overwrite(tmp_path: Path) -> None:
    server, db, _store = _start_test_server(tmp_path)
    try:
        original = ProfileConfig(
            profile_uid="profile-1",
            name="Global Profile A",
            driver_key="apc_modbus_smt",
            config_payload={"driver_key": "apc_modbus_smt", "poll_groups": {"slow": 60}},
            selected_sensors=["runtime_remaining"],
            sensor_preferences={"runtime_remaining": {"mqtt_enabled": True, "poll_group": "slow"}},
            comments="",
            is_protected=False,
        )
        db.save_profile(original)
        payload = _base_export_payload()
        payload["profiles"] = [
            {
                "profile_uid": "profile-1",
                "name": "Global Profile A",
                "driver_key": "apc_modbus_smt",
                "config_payload": {"driver_key": "apc_modbus_smt", "poll_groups": {"slow": 30}},
                "selected_sensors": ["runtime_remaining", "output_voltage"],
                "sensor_preferences": {"runtime_remaining": {"mqtt_enabled": True, "poll_group": "slow"}},
                "comments": "changed",
                "is_protected": False,
            }
        ]
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, _headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(payload)},
        )
        assert status == HTTPStatus.BAD_REQUEST
        unchanged = next(item for item in db.load_profiles() if item.profile_uid == "profile-1")
        assert unchanged.comments == ""
        assert unchanged.config_payload["poll_groups"]["slow"] == 60
    finally:
        server.shutdown()
        server.server_close()


def test_export_import_local_profile_preserves_local_fields(tmp_path: Path) -> None:
    export_server, export_db, export_store = _start_test_server(tmp_path / "export")
    try:
        profile = ProfileConfig(
            profile_uid="profile-1",
            name="Global Profile A",
            driver_key="apc_modbus_smt",
            config_payload={"driver_key": "apc_modbus_smt", "poll_groups": {"slow": 60}},
            selected_sensors=["runtime_remaining"],
            sensor_preferences={"runtime_remaining": {"mqtt_enabled": True, "poll_group": "slow"}},
            comments="",
            is_protected=False,
        )
        export_db.save_profile(profile)
        export_store.upsert(
            DeviceConfig(
                id="dev-local",
                source="apc_modbus_smt",
                host="10.0.0.20",
                device_uid="device-local",
                profile_uid="profile-1",
                profile_mode="local",
                local_profile_payload={"driver_key": "apc_modbus_smt", "poll_groups": {"slow": 45}, "key_precedence": {}},
                local_selected_sensors=["runtime_remaining"],
                local_sensor_preferences={"runtime_remaining": {"mqtt_enabled": True, "poll_group": "slow"}},
            )
        )
        base_url = f"http://127.0.0.1:{export_server.server_port}"
        status, body, _headers = _fetch(base_url, "/htmx/maintenance/backup/export")
        assert status == HTTPStatus.OK
        exported = json.loads(body)
    finally:
        export_server.shutdown()
        export_server.server_close()

    import_server, import_db, _import_store = _start_test_server(tmp_path / "import")
    try:
        base_url = f"http://127.0.0.1:{import_server.server_port}"
        status, _body, _headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(exported)},
        )
        assert status == HTTPStatus.OK
        imported = next(item for item in import_db.load_devices() if item.device_uid == "device-local")
        assert imported.profile_mode == "local"
        assert imported.local_profile_payload == {"driver_key": "apc_modbus_smt", "poll_groups": {"slow": 45}, "key_precedence": {}}
        assert imported.local_selected_sensors == ["runtime_remaining"]
        assert imported.local_sensor_preferences == {
            "runtime_remaining": {"mqtt_enabled": True, "poll_group": "slow"}
        }
    finally:
        import_server.shutdown()
        import_server.server_close()


def test_export_import_default_profile_device_stays_default_mode(tmp_path: Path) -> None:
    export_server, _export_db, export_store = _start_test_server(tmp_path / "export-default")
    try:
        export_store.upsert(
            DeviceConfig(
                id="dev-default",
                source="apc_modbus_smt",
                host="10.0.0.30",
                device_uid="device-default",
                profile_uid="",
                profile_mode="default",
            )
        )
        base_url = f"http://127.0.0.1:{export_server.server_port}"
        status, body, _headers = _fetch(base_url, "/htmx/maintenance/backup/export")
        assert status == HTTPStatus.OK
        exported = json.loads(body)
        device_row = exported["devices"][0]
        assert device_row["profile_mode"] == "default"
        assert device_row["profile_uid"] is None
    finally:
        export_server.shutdown()
        export_server.server_close()

    import_server, import_db, _import_store = _start_test_server(tmp_path / "import-default")
    try:
        base_url = f"http://127.0.0.1:{import_server.server_port}"
        status, _body, _headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(exported)},
        )
        assert status == HTTPStatus.OK
        imported = next(item for item in import_db.load_devices() if item.device_uid == "device-default")
        assert imported.profile_mode == "default"
        assert imported.profile_uid == ""
        assert imported.source == "apc_modbus_smt"
    finally:
        import_server.shutdown()
        import_server.server_close()


def test_import_mismatched_driver_profile_is_rejected(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        payload = _base_export_payload()
        payload["profiles"] = [
            {
                "profile_uid": "profile-1",
                "name": "Profile A",
                "driver_key": "apc_modbus_smt",
                "config_payload": {"driver_key": "apc_modbus_smt", "poll_groups": {"slow": 60}},
                "selected_sensors": ["runtime_remaining"],
                "sensor_preferences": {"runtime_remaining": {"mqtt_enabled": True, "poll_group": "slow"}},
                "comments": "",
                "is_protected": False,
            }
        ]
        payload["devices"] = [
            {
                "device_uid": "device-1",
                "name": "UPS A",
                "driver_key": "cyberpower_modbus_single_phase",
                "profile_mode": "global",
                "profile_uid": "profile-1",
                "profile_name": "Profile A",
                "config": {
                    "id": "dev-a",
                    "host": "10.0.0.10",
                    "port": 502,
                    "unit_id": 1,
                    "snmp_community": "public",
                    "poll_interval": None,
                    "debug_logging": False,
                    "keep_connection_open": False,
                    "discovery_enabled": True,
                    "polling_enabled": True,
                },
                "local_profile_payload": None,
                "local_selected_sensors": None,
                "local_sensor_preferences": None,
            }
        ]
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, _headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(payload)},
        )
        assert status == HTTPStatus.BAD_REQUEST
    finally:
        server.shutdown()
        server.server_close()


def test_csv_import_legacy_path_still_supported(tmp_path: Path) -> None:
    server, db, _store = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        csv_payload = (
            "ID,Source,Host,Port,Unit,SNMP,Poll,Name,Debug,KeepConnectionOpen,Discovery,Polling\n"
            "legacy-1,apc_modbus_smt,127.0.0.1,502,1,public,,Legacy,false,false,true,true"
        )
        status, _body, _headers = _post(
            base_url,
            "/htmx/maintenance/import/csv",
            {"csv_file": csv_payload},
        )
        assert status == HTTPStatus.OK
        assert any(item.id == "legacy-1" for item in db.load_devices())
    finally:
        server.shutdown()
        server.server_close()


def test_csv_import_template_route_returns_headers_only(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body, headers = _fetch(base_url, "/htmx/maintenance/import/template.csv")
        assert status == HTTPStatus.OK
        assert headers.get("Content-Type", "").startswith("text/csv")
        assert body == (
            "ID,Source,Host,Port,SNMPPort,Unit,SNMP,Poll,Name,Location,Debug,KeepConnectionOpen,Discovery,Polling\n"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_csv_export_endpoint_removed(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, _headers = _fetch(base_url, "/export-csv")
        assert status == HTTPStatus.NOT_FOUND
    finally:
        server.shutdown()
        server.server_close()


def test_maintenance_shows_backup_restore_and_devices_hides_import_export(
    tmp_path: Path,
) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        maintenance_status, maintenance_body, _maintenance_headers = _fetch(
            base_url, "/htmx/devices/partials/panel/maintenance"
        )
        assert maintenance_status == HTTPStatus.OK
        assert "Backup and Restore" in maintenance_body
        assert "Download JSON Backup" in maintenance_body
        assert "Restore from JSON Backup" in maintenance_body
        assert "Download CSV Import Template" in maintenance_body
        assert "Import CSV" in maintenance_body

        devices_status, devices_body, _devices_headers = _fetch(
            base_url, "/htmx/devices/partials/panel/devices"
        )
        assert devices_status == HTTPStatus.OK
        assert "Export JSON" not in devices_body
        assert "Import JSON" not in devices_body
        assert "Import CSV" not in devices_body
    finally:
        server.shutdown()
        server.server_close()


def test_backup_import_rejects_missing_schema(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        payload = _base_export_payload()
        payload.pop("schema", None)
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(payload)},
        )
        assert status == HTTPStatus.BAD_REQUEST
        assert "missing 'schema'" in headers.get("HX-Trigger", "")
    finally:
        server.shutdown()
        server.server_close()


def test_backup_import_rejects_wrong_schema(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        payload = _base_export_payload()
        payload["schema"] = "ups2mqtt.other_export"
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(payload)},
        )
        assert status == HTTPStatus.BAD_REQUEST
        assert "Invalid schema" in headers.get("HX-Trigger", "")
    finally:
        server.shutdown()
        server.server_close()


def test_backup_import_rejects_missing_version(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        payload = _base_export_payload()
        payload.pop("version", None)
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(payload)},
        )
        assert status == HTTPStatus.BAD_REQUEST
        assert "missing 'version'" in headers.get("HX-Trigger", "")
    finally:
        server.shutdown()
        server.server_close()


def test_backup_import_rejects_future_version(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        payload = _base_export_payload()
        payload["version"] = BACKUP_SCHEMA_VERSION + 1
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(payload)},
        )
        assert status == HTTPStatus.BAD_REQUEST
        assert "Unsupported version" in headers.get("HX-Trigger", "")
    finally:
        server.shutdown()
        server.server_close()


def test_sidebar_versions_block_shows_app_and_backup_versions(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body, _headers = _fetch(base_url, "/htmx/devices")
        assert status == HTTPStatus.OK
        assert "Versions" in body
        assert f"App:</strong> {APP_VERSION}" in body
        assert (
            f"Backup schema:</strong> {BACKUP_SCHEMA_NAME} v{BACKUP_SCHEMA_VERSION}"
            in body
        )
    finally:
        server.shutdown()
        server.server_close()


def test_legacy_non_htmx_get_route_returns_not_found(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, _headers = _fetch(base_url, "/legacy-devices")
        assert status == HTTPStatus.NOT_FOUND
    finally:
        server.shutdown()
        server.server_close()


def test_legacy_non_htmx_post_action_returns_not_found(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, _body, _headers = _post(base_url, "/", {"action": "toggle_debug", "id": "dev-1"})
        assert status == HTTPStatus.NOT_FOUND
    finally:
        server.shutdown()
        server.server_close()


def test_devices_table_includes_location_column_and_dash_for_empty_location(
    tmp_path: Path,
) -> None:
    server, _db, store = _start_test_server(tmp_path)
    try:
        store.upsert(
            DeviceConfig(
                id="dev-loc",
                source="apc_modbus_smt",
                host="10.0.0.50",
                name="UPS With Location",
                location="Rack A",
                device_uid="device-loc",
            )
        )
        store.upsert(
            DeviceConfig(
                id="dev-no-loc",
                source="apc_modbus_smt",
                host="10.0.0.51",
                name="UPS No Location",
                device_uid="device-no-loc",
            )
        )
        base_url = f"http://127.0.0.1:{server.server_port}"
        status, body, _headers = _fetch(base_url, "/htmx/devices/partials/table")
        assert status == HTTPStatus.OK
        assert "<th>Location</th>" in body
        assert "Rack A" in body
        assert "text-muted\">-</span>" in body
    finally:
        server.shutdown()
        server.server_close()


def test_upsert_device_persists_location_and_updates_location(tmp_path: Path) -> None:
    server, db, _store = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        create_payload = {
            "id": "dev-form",
            "source": "apc_modbus_smt",
            "profile_mode": "local",
            "host": "10.0.0.60",
            "port": "502",
            "unit_id": "1",
            "snmp_community": "public",
            "poll_interval": "",
            "name": "Form UPS",
            "location": "Closet 1",
            "discovery_enabled": "on",
            "polling_enabled": "on",
        }
        create_status, _create_body, _create_headers = _post(
            base_url, "/htmx/devices/actions/upsert", create_payload
        )
        assert create_status == HTTPStatus.OK
        created = next(item for item in db.load_devices() if item.id == "dev-form")
        assert created.location == "Closet 1"

        update_payload = {
            "id": "dev-form",
            "original_id": "dev-form",
            "device_uid": created.device_uid,
            "source": "apc_modbus_smt",
            "profile_mode": "local",
            "host": "10.0.0.60",
            "port": "502",
            "unit_id": "1",
            "snmp_community": "public",
            "poll_interval": "",
            "name": "Form UPS",
            "location": "Closet 2",
            "discovery_enabled": "on",
            "polling_enabled": "on",
        }
        update_status, _update_body, _update_headers = _post(
            base_url, "/htmx/devices/actions/upsert", update_payload
        )
        assert update_status == HTTPStatus.OK
        updated = next(item for item in db.load_devices() if item.id == "dev-form")
        assert updated.location == "Closet 2"
    finally:
        server.shutdown()
        server.server_close()


def test_json_export_import_preserves_location(tmp_path: Path) -> None:
    export_server, _export_db, export_store = _start_test_server(tmp_path / "export-location")
    try:
        export_store.upsert(
            DeviceConfig(
                id="dev-loc-json",
                source="apc_modbus_smt",
                host="10.0.0.70",
                name="JSON UPS",
                location="Warehouse 5",
                device_uid="device-loc-json",
                profile_mode="default",
            )
        )
        base_url = f"http://127.0.0.1:{export_server.server_port}"
        status, body, _headers = _fetch(base_url, "/htmx/maintenance/backup/export")
        assert status == HTTPStatus.OK
        exported = json.loads(body)
        assert exported["devices"][0]["location"] == "Warehouse 5"
    finally:
        export_server.shutdown()
        export_server.server_close()

    import_server, import_db, _import_store = _start_test_server(tmp_path / "import-location")
    try:
        base_url = f"http://127.0.0.1:{import_server.server_port}"
        status, _body, _headers = _post(
            base_url,
            "/htmx/maintenance/backup/import",
            {"json_file": json.dumps(exported)},
        )
        assert status == HTTPStatus.OK
        imported = next(
            item for item in import_db.load_devices() if item.device_uid == "device-loc-json"
        )
        assert imported.location == "Warehouse 5"
    finally:
        import_server.shutdown()
        import_server.server_close()


def test_csv_import_supports_location_column(tmp_path: Path) -> None:
    server, db, _store = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        csv_payload = (
            "ID,Source,Host,Port,Unit,SNMP,Poll,Name,Location,Debug,KeepConnectionOpen,Discovery,Polling\n"
            "legacy-2,apc_modbus_smt,127.0.0.2,502,1,public,,Legacy Two,DC Room,false,false,true,true"
        )
        status, _body, _headers = _post(
            base_url,
            "/htmx/maintenance/import/csv",
            {"csv_file": csv_payload},
        )
        assert status == HTTPStatus.OK
        imported = next(item for item in db.load_devices() if item.id == "legacy-2")
        assert imported.location == "DC Room"
    finally:
        server.shutdown()
        server.server_close()


def test_csv_import_multipart_cp1252_payload_does_not_crash(tmp_path: Path) -> None:
    server, _db, _store = _start_test_server(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        boundary = "----ups2mqtt-test-boundary"
        csv_bytes = (
            b"\x95 ID,Source,Host,Port,Unit,SNMP,Poll,Name,Location,Debug,KeepConnectionOpen,Discovery,Polling\r\n"
            b"legacy-3,apc_modbus_smt,127.0.0.3,502,1,public,,Legacy Three,Lab,false,false,true,true\r\n"
        )
        multipart_body = (
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="csv_file"; filename="devices.csv"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            ).encode("ascii")
            + csv_bytes
            + f"\r\n--{boundary}--\r\n".encode("ascii")
        )
        request = Request(
            f"{base_url}/htmx/maintenance/import/csv",
            data=multipart_body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urlopen(request) as response:  # nosec B310
            assert int(response.status) == HTTPStatus.OK
    finally:
        server.shutdown()
        server.server_close()
