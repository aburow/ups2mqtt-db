# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from typing import Any

DEFAULT_POLL_INTERVAL_SECONDS = 15


def clamp_poll_interval(
    value: Any,
    minimum: int = DEFAULT_POLL_INTERVAL_SECONDS,
) -> int:
    return max(int(minimum), int(value))


def clamp_optional_poll_interval(
    value: Any,
    minimum: int = DEFAULT_POLL_INTERVAL_SECONDS,
) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return clamp_poll_interval(value, minimum)
