# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
from pathlib import Path

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INSERT_RE = re.compile(r'^INSERT INTO "?([A-Za-z_][A-Za-z0-9_]*)"?\s')


def _sql_text_literal(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _capability_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name LIKE 'capability_%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row["name"]) for row in rows]


def _safe_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _capability_insert_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in conn.iterdump():
        text = line.strip()
        match = _INSERT_RE.match(text)
        if not match:
            continue
        table = str(match.group(1))
        if not table.startswith("capability_"):
            continue
        out.setdefault(table, []).append(text if text.endswith(";") else f"{text};")
    return out


def _default_profile_inserts(conn: sqlite3.Connection) -> list[str]:
    has_profiles_table = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'profiles'
        """
    ).fetchone()
    if not has_profiles_table:
        return []

    rows = conn.execute(
        """
        SELECT
            profile_uid,
            name,
            driver_key,
            config_payload,
            selected_sensors,
            sensor_preferences,
            comments,
            is_protected,
            created_at,
            updated_at
        FROM profiles
        WHERE name LIKE '%[default]%'
        ORDER BY updated_at, profile_uid
        """
    ).fetchall()
    inserts: list[str] = []
    for row in rows:
        profile_uid = str(row["profile_uid"] or "")
        name = str(row["name"] or "")
        driver_key = str(row["driver_key"] or "")
        config_payload = str(row["config_payload"] or "{}")
        selected_sensors = str(row["selected_sensors"] or "[]")
        sensor_preferences = _sql_text_literal(
            None
            if row["sensor_preferences"] is None
            else str(row["sensor_preferences"])
        )
        comments = _sql_text_literal(
            None if row["comments"] is None else str(row["comments"])
        )
        is_protected = int(row["is_protected"] or 0)
        created_at = _sql_text_literal(
            None if row["created_at"] is None else str(row["created_at"])
        )
        updated_at = _sql_text_literal(
            None if row["updated_at"] is None else str(row["updated_at"])
        )
        inserts.append(
            "INSERT OR REPLACE INTO profiles("
            "profile_uid, name, driver_key, config_payload, selected_sensors, "
            "sensor_preferences, comments, is_protected, created_at, updated_at"
            ") VALUES ("
            f"{_sql_text_literal(profile_uid)}, "
            f"{_sql_text_literal(name)}, "
            f"{_sql_text_literal(driver_key)}, "
            f"{_sql_text_literal(config_payload)}, "
            f"{_sql_text_literal(selected_sensors)}, "
            f"{sensor_preferences}, "
            f"{comments}, "
            f"{is_protected}, "
            f"{created_at}, "
            f"{updated_at}"
            ");"
        )
    return inserts


def dump_capability_snapshot(*, db_path: str, output_path: str) -> None:
    conn = _connect(db_path)
    try:
        tables = _capability_tables(conn)
        if not tables:
            raise ValueError("No capability_* tables found in database")
        for table in tables:
            _safe_identifier(table)
        inserts_by_table = _capability_insert_map(conn)

        now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines: list[str] = [
            "-- ups2mqtt capability snapshot",
            f"-- generated_at_utc: {now_utc}",
            f"-- source_db: {db_path}",
            "",
            "PRAGMA foreign_keys=OFF;",
            "BEGIN;",
            "",
        ]

        for table in reversed(tables):
            lines.append(f"DELETE FROM {table};")
        lines.append("DELETE FROM profiles WHERE name LIKE '%[default]%';")
        lines.append("")

        for table in tables:
            inserts = inserts_by_table.get(table, [])
            lines.append(f"-- {table} rows: {len(inserts)}")
            lines.extend(inserts)
            lines.append("")

        default_profile_inserts = _default_profile_inserts(conn)
        lines.append(f"-- profiles default rows: {len(default_profile_inserts)}")
        lines.extend(default_profile_inserts)
        lines.append("")

        lines.extend(
            [
                "COMMIT;",
                "PRAGMA foreign_keys=ON;",
                "",
            ]
        )

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
    finally:
        conn.close()


def prime_capability_snapshot(*, db_path: str, snapshot_path: str) -> None:
    # Ensure schema exists so snapshot can be applied to a brand-new DB path.
    from .database import Database

    Database(db_path=db_path).close()
    sql_text = Path(snapshot_path).read_text(encoding="utf-8")
    conn = _connect(db_path)
    try:
        conn.executescript(sql_text)
        conn.commit()
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ups2mqtt.db_snapshot",
        description="Dump/prime capability_* database snapshot",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    dump_parser = subparsers.add_parser("dump", help="Dump capability snapshot SQL")
    dump_parser.add_argument("--db", required=True, help="Path to SQLite database")
    dump_parser.add_argument("--out", required=True, help="Output SQL file path")

    prime_parser = subparsers.add_parser("prime", help="Prime database from SQL dump")
    prime_parser.add_argument("--db", required=True, help="Path to SQLite database")
    prime_parser.add_argument("--in", dest="in_path", required=True, help="Input SQL")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.cmd == "dump":
        dump_capability_snapshot(db_path=args.db, output_path=args.out)
        print(f"Snapshot written: {args.out}")
        return 0
    if args.cmd == "prime":
        prime_capability_snapshot(db_path=args.db, snapshot_path=args.in_path)
        print(f"Snapshot applied: {args.in_path} -> {args.db}")
        return 0

    parser.error(f"Unsupported command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
