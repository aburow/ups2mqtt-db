# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import asyncio
import logging
import math
from time import monotonic, time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .model import DeviceConfig

LOG = logging.getLogger("ups2mqtt.telemetry_influx")


def _escape_measurement(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _escape_tag_part(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace("=", "\\=")
        .replace(" ", "\\ ")
    )


def _escape_field_key(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def format_line_protocol_point(
    *,
    measurement: str,
    device_id: str,
    source: str,
    field_key: str,
    value: int | float,
    timestamp_ms: int,
) -> str:
    measurement_escaped = _escape_measurement(measurement)
    tags = (
        f"device_id={_escape_tag_part(device_id)},"
        f"source={_escape_tag_part(source)},"
        f"field_key={_escape_tag_part(field_key)}"
    )
    field_name = _escape_field_key("value")
    if isinstance(value, bool):
        raise ValueError("bool values are not supported")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("non-finite numeric value")
    # Keep a stable field type in Influx by always writing numeric telemetry as float.
    field_value = f"{numeric:.15g}"
    # precision=millisecond expects millisecond timestamps, not nanoseconds.
    timestamp = int(timestamp_ms)
    return f"{measurement_escaped},{tags} {field_name}={field_value} {timestamp}"


class InfluxV3TelemetryExporter:
    def __init__(
        self,
        *,
        url: str,
        database: str,
        token: str | None,
        measurement: str,
        timeout_seconds: float,
        queue_size: int,
        flush_interval_seconds: float,
        batch_max_points: int,
        accept_partial: bool,
    ) -> None:
        self._url = str(url).strip().rstrip("/")
        self._database = str(database).strip()
        self._token = str(token).strip() if token else ""
        self._measurement = str(measurement).strip() or "ups2mqtt"
        self._timeout_seconds = max(0.1, float(timeout_seconds))
        self._flush_interval_seconds = max(0.1, float(flush_interval_seconds))
        self._batch_max_points = max(1, int(batch_max_points))
        self._accept_partial = bool(accept_partial)
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max(1, int(queue_size)))
        self._drop_count = 0
        self._last_drop_log_at = 0.0
        self._endpoint = f"{self._url}/api/v3/write_lp"
        self._params = {
            "db": self._database,
            "precision": "millisecond",
            "accept_partial": "true" if self._accept_partial else "false",
        }

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._database)

    @property
    def endpoint_for_log(self) -> str:
        return f"{self._endpoint}?{urlencode(self._params)}"

    def enqueue_device_values(
        self,
        *,
        device: DeviceConfig,
        runtime_source: str,
        values: dict[str, Any],
    ) -> None:
        now_ms = int(time() * 1000)
        for field_key, raw_value in values.items():
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                continue
            numeric = float(raw_value)
            if not math.isfinite(numeric):
                continue
            try:
                point = format_line_protocol_point(
                    measurement=self._measurement,
                    device_id=str(device.id),
                    source=str(runtime_source or device.source),
                    field_key=str(field_key),
                    value=raw_value,
                    timestamp_ms=now_ms,
                )
                self._queue.put_nowait(point)
            except asyncio.QueueFull:
                self._drop_count += 1
                now = monotonic()
                if now - self._last_drop_log_at >= 30.0:
                    LOG.warning(
                        "Influx telemetry queue full; dropped %d points",
                        self._drop_count,
                    )
                    self._last_drop_log_at = now
            except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                LOG.debug("Influx enqueue skipped malformed point: %s", err)

    async def _post_lines(
        self,
        session: aiohttp.ClientSession,
        lines: list[str],
    ) -> None:
        if not lines:
            return
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        body = "\n".join(lines)
        async with session.post(
            self._endpoint,
            params=self._params,
            data=body.encode("utf-8"),
            headers=headers,
        ) as response:
            if response.status >= 300:
                response_text = (await response.text())[:400]
                LOG.warning(
                    "Influx write failed status=%d endpoint=%s error=%s",
                    response.status,
                    self.endpoint_for_log,
                    response_text,
                )

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        connector = aiohttp.TCPConnector(limit=4, enable_cleanup_closed=True)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            batch: list[str] = []
            try:
                while True:
                    try:
                        line = await asyncio.wait_for(
                            self._queue.get(), timeout=self._flush_interval_seconds
                        )
                        batch.append(line)
                        if len(batch) < self._batch_max_points:
                            continue
                    except TimeoutError:
                        pass
                    if not batch:
                        continue
                    try:
                        await self._post_lines(session, batch)
                    except Exception as err:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                        LOG.warning(
                            "Influx write failed endpoint=%s error=%s",
                            self.endpoint_for_log,
                            err,
                        )
                    finally:
                        batch.clear()
            except asyncio.CancelledError:
                if batch:
                    try:
                        await asyncio.wait_for(self._post_lines(session, batch), timeout=1.0)
                    except Exception:  # noqa: BLE001  # grain: ignore NAKED_EXCEPT
                        pass
                raise
