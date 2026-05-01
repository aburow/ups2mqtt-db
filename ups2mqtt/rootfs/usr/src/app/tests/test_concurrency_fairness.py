from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ups2mqtt.concurrency import AdjustableConcurrencyLimiter


def test_limiter_rebalances_grants_when_one_source_is_overrepresented() -> None:
    async def scenario() -> dict:
        limiter = AdjustableConcurrencyLimiter(4)
        for _ in range(4):
            await limiter.acquire("source_a")

        granted: list[str] = []

        async def wait_for_slot(source: str) -> None:
            await limiter.acquire(source)
            granted.append(source)

        tasks = [
            *(asyncio.create_task(wait_for_slot("source_a")) for _ in range(4)),
            *(asyncio.create_task(wait_for_slot("source_b")) for _ in range(4)),
        ]
        try:
            await asyncio.sleep(0.05)
            for _ in range(4):
                await limiter.release("source_a")
                await asyncio.sleep(0.05)

            snapshot = limiter.snapshot()
            return {
                "granted": granted,
                "in_flight_by_source": snapshot["in_flight_by_source"],
                "fair_source_limit": snapshot["fair_source_limit"],
            }
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    result = asyncio.run(scenario())

    assert result["granted"].count("source_b") == 2
    assert result["granted"].count("source_a") == 2
    assert result["in_flight_by_source"] == {"source_a": 2, "source_b": 2}
    assert result["fair_source_limit"] == 2
