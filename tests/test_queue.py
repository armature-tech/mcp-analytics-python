"""Tests for the privacy queue overflow path.

The TypeScript SDK has tests/queue.test.ts and the Go SDK has queue_test.go. Python had no
queue test, which is why the overflow warning could report the wrong number unnoticed.
"""

from __future__ import annotations

import asyncio
import warnings

from armature_mcp_analytics.queue import PRIVACY_QUEUE_CAPACITY, create_privacy_queue


def _config() -> dict:
    """A queue that never delivers, so enqueue is the only thing under test."""
    return {"enabled": True, "delivery": "background", "emit": lambda batch: None,
            "schedule": lambda work: None}


def test_overflow_warning_does_not_report_a_count_that_is_always_one() -> None:
    queue = create_privacy_queue(_config())

    async def fill() -> None:
        # One past capacity is enough to warn; the rest prove the message does not claim
        # a total it cannot know at the moment it fires.
        for _ in range(PRIVACY_QUEUE_CAPACITY + 50):
            await queue.enqueue(lambda: None)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(fill())

    overflow = [str(w.message) for w in caught if "privacy queue overflow" in str(w.message)]
    assert len(overflow) == 1, f"the warning should fire once, got {len(overflow)}"
    # Before this change the message read "dropped 1 oldest candidate(s)" after 50 drops,
    # because the warning fires on the first one and the counter is interpolated there.
    assert "dropped 1 " not in overflow[0], overflow[0]
    assert "further drops are counted but not warned" in overflow[0], overflow[0]
