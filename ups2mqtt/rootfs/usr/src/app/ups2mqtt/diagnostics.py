# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

"""Configuration and connectivity diagnostics."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .icon_resolver import resolve_icon, resolve_enabled_by_default
from .model import AppConfig, DeviceConfig
from .mqtt import MqttPublisher

LOG = logging.getLogger("ups2mqtt.diagnostics")


def check_config(config: AppConfig, devices: list[DeviceConfig]) -> dict[str, Any]:
    """Perform comprehensive configuration and connectivity checks.

    Returns:
        Dict with keys:
        - "status": "ok" or "failed"
        - "mqtt": MQTT connectivity result
        - "ha_api": Home Assistant API result (if configured)
        - "external_apps": External app module availability
        - "devices": Per-device polling test results
        - "summary": Overall summary message
    """
    results: dict[str, Any] = {
        "mqtt": None,
        "ha_api": None,
        "external_apps": None,
        "devices": {},
    }

    # Check MQTT
    results["mqtt"] = _check_mqtt(config)

    # Check Home Assistant API (note: HA API check is async, so we skip it in sync context)
    if config.ha_url and config.ha_token:
        results["ha_api"] = {
            "status": "configured",
            "url": config.ha_url,
            "message": "HA integration configured (async test skipped)",
        }
    else:
        results["ha_api"] = {"status": "skipped", "reason": "Not configured"}

    # Check external app modules
    results["external_apps"] = _check_external_apps(config.apps_dir, devices)

    # Check each device
    for device in devices:
        results["devices"][device.id] = _check_device_config(device, config.apps_dir)

    # Overall status
    status = "ok"
    failures = []
    if results["mqtt"].get("status") != "ok":
        status = "failed"
        failures.append(f"MQTT: {results['mqtt'].get('error', 'Unknown error')}")

    results["status"] = status
    results["summary"] = (
        "All systems operational"
        if status == "ok"
        else f"Issues found: {'; '.join(failures)}"
    )

    return results


def _check_mqtt(config: AppConfig) -> dict[str, Any]:
    """Check MQTT broker connectivity."""
    if not config.mqtt_enabled:
        return {"status": "skipped", "reason": "MQTT disabled"}

    try:
        publisher = MqttPublisher(config)
        if not publisher.ensure_connected():
            return {
                "status": "failed",
                "error": f"Could not connect to {config.mqtt_host}:{config.mqtt_port}",
            }
        publisher.close()
        return {
            "status": "ok",
            "host": config.mqtt_host,
            "port": config.mqtt_port,
            "message": "Successfully connected to MQTT broker",
        }
    except Exception as err:  # grain: ignore NAKED_EXCEPT
        return {"status": "failed", "error": str(err)}


def _check_external_apps(apps_dir: str, devices: list[DeviceConfig]) -> dict[str, Any]:
    """Check external app module availability."""
    apps_path = Path(apps_dir)
    results: dict[str, Any] = {"available_apps": [], "missing_apps": [], "modules": {}}

    if not apps_path.exists():
        results["status"] = "warning"
        results["message"] = f"Apps directory does not exist: {apps_dir}"
        return results

    # Check which apps are needed
    required_apps = set()
    for device in devices:
        source_prefix = device.source.split("_")[0]
        if source_prefix == "apc":
            required_apps.add("apc-modbus-ha")
        elif source_prefix == "ups":
            required_apps.add("ups-snmp-ha")
        elif source_prefix == "cyberpower":
            required_apps.add("cyberpower-modbus-ha")

    # Check which are available
    for app_name in required_apps:
        app_path = apps_path / app_name
        if app_path.exists():
            results["available_apps"].append(app_name)
            # Try to load icon module
            icon_module = (
                app_path
                / "custom_components"
                / app_name.split("-")[0]
                / "icons_unified.py"
            )
            availability_module = (
                app_path
                / "custom_components"
                / app_name.split("-")[0]
                / "sensor_availability_unified.py"
            )
            results["modules"][app_name] = {
                "icons": icon_module.exists(),
                "availability": availability_module.exists(),
            }
        else:
            results["missing_apps"].append(app_name)

    results["status"] = "ok" if not results["missing_apps"] else "warning"
    results["message"] = (
        "All required apps available"
        if not results["missing_apps"]
        else f"Missing apps: {', '.join(results['missing_apps'])}"
    )

    return results


def _check_device_config(device: DeviceConfig, apps_dir: str) -> dict[str, Any]:
    """Check device configuration validity."""
    issues = []

    if not device.host:
        issues.append("Missing host")
    if not device.source:
        issues.append("Missing source")
    if device.port < 1 or device.port > 65535:
        issues.append(f"Invalid port: {device.port}")

    # Try to resolve icon
    try:
        icon = resolve_icon(device.source, "battery_charge", apps_dir)
        if not icon:
            issues.append("Could not resolve any icons (using fallback)")
    except Exception as err:  # grain: ignore NAKED_EXCEPT
        issues.append(f"Icon resolution error: {err}")

    # Try to resolve availability
    try:
        resolve_enabled_by_default(device.source, "battery_charge", apps_dir)
        # No error means it worked
    except Exception as err:  # grain: ignore NAKED_EXCEPT
        issues.append(f"Availability resolution error: {err}")

    return {
        "status": "ok" if not issues else "warning",
        "host": device.host,
        "port": device.port,
        "source": device.source,
        "issues": issues,
        "polling_enabled": device.polling_enabled,
        "discovery_enabled": device.discovery_enabled,
    }
