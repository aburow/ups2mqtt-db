#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(_SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_ROOT))

from ups2mqtt.capabilities import _collect_metric_keys, load_capabilities  # noqa: E402
from ups2mqtt.capability_repository import get_capability_repository  # noqa: E402
from ups2mqtt.icon_resolver import resolve_enabled_defaults  # noqa: E402

STATUS_SUFFIXES = (
    "_status",
    "_source",
    "_state",
    "_result",
    "_fail",
    "_fault",
    "_present",
    "_active",
    "_muted",
    "_off",
    "_on",
    "_eod",
    "_untrack",
    "_timeout",
    "_shorted",
    "_connected",
    "_low",
    "_bf",
)


def _load_rows() -> dict[str, list[dict[str, Any]]]:
    payload = load_capabilities()
    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict):
        return {}
    repo = get_capability_repository()
    rows_by_driver: dict[str, list[dict[str, Any]]] = {}
    for driver_key in sorted(profiles):
        rows_by_driver[str(driver_key)] = repo.load_catalog_sensor_rows(str(driver_key))
    return rows_by_driver


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit default-enabled sensors that are missing unit metadata."
    )
    parser.add_argument(
        "--apps-dir",
        default=os.environ.get("UPS_UNIFIED_APPS_DIR", "/data/apps"),
        help="Apps directory used for default-enable resolution.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail if any default-enabled core/measurement sensors are missing units.",
    )
    parser.add_argument(
        "--db-path",
        default=str(Path("/tmp/ups2mqtt-audit.db")),
        help="Temporary sqlite path to use while loading capabilities.",
    )
    args = parser.parse_args()

    os.environ.setdefault("UPS_UNIFIED_DB_PATH", args.db_path)
    os.environ.setdefault("UPS_UNIFIED_APPS_DIR", args.apps_dir)

    payload = load_capabilities()
    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict):
        print("No profiles available")
        return 1
    rows_by_driver = _load_rows()

    findings: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    strict_findings = 0

    for driver_key, profile in sorted(profiles.items()):
        keys = sorted(_collect_metric_keys(profile))
        defaults = resolve_enabled_defaults(
            str(driver_key),
            keys,
            apps_dir=args.apps_dir,
            authoritative=False,
        )
        row_by_key = {
            str(row.get("key", "")): row for row in rows_by_driver.get(str(driver_key), [])
        }
        for key in keys:
            if not bool(defaults.get(key, True)):
                continue
            row = row_by_key.get(key)
            if row is None:
                continue
            if str(row.get("unit", "")).strip():
                continue
            category = str(row.get("category", "")).strip().lower()
            label = str(row.get("label", key))
            findings[str(driver_key)].append((key, category, label))
            if category in {"core", "measurement"} and not key.lower().endswith(
                STATUS_SUFFIXES
            ):
                strict_findings += 1

    if not findings:
        print("No default-enabled sensors with missing units were found.")
        return 0

    for driver_key in sorted(findings):
        print(f"[{driver_key}]")
        for key, category, label in sorted(findings[driver_key]):
            print(f"  - {key} | category={category} | label={label}")

    print("")
    print(f"Total findings: {sum(len(v) for v in findings.values())}")
    print(f"Strict findings (core/measurement): {strict_findings}")

    if args.strict and strict_findings > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
