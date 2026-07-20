from __future__ import annotations

import asyncio
import inspect
import os
import warnings
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from .emit import _config_value, post_telemetry_event
from .types import AnalyticsConfig, AnalyticsIngestBatch, AnalyticsIngestEvent
from .utils import SCHEMA_VERSION

PRIVACY_QUEUE_CAPACITY = 1_000
PRIVACY_QUEUE_BATCH_SIZE = 20

PrivacyQueueFinalizer = Callable[
    [],
    list[AnalyticsIngestEvent]
    | None
    | Awaitable[list[AnalyticsIngestEvent] | None],
]

_warned_serverless_without_schedule = False


class PrivacyQueue:
    def __init__(
        self,
        config: AnalyticsConfig | None = None,
    ) -> None:
        global _warned_serverless_without_schedule

        self.config = config or {}
        self._pending: deque[PrivacyQueueFinalizer] = deque()
        self._running: asyncio.Task[None] | None = None
        self._dropped = 0
        self._warned_dropped = False

        if (
            self._enabled()
            and self._delivery() != "await"
            and self._schedule_hook() is None
            and not _warned_serverless_without_schedule
            and (os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("VERCEL"))
        ):
            _warned_serverless_without_schedule = True
            self._warn(
                'background delivery may be frozen by this serverless runtime; use delivery: "await" or armature.schedule'
            )

    @staticmethod
    def _warn(message: str) -> None:
        warnings.warn(f"[mcp-analytics] {message}", RuntimeWarning, stacklevel=3)

    def _enabled(self) -> bool:
        return _config_value(self.config, "enabled", "enabled", True) is not False

    def _delivery(self) -> str:
        return str(_config_value(self.config, "delivery", "delivery", "background"))

    def _schedule_hook(self) -> Callable[[Awaitable[None]], Any] | None:
        hook = _config_value(self.config, "schedule", "schedule")
        return hook if callable(hook) else None

    async def _emit(self, batch: AnalyticsIngestBatch) -> None:
        try:
            emitter = _config_value(self.config, "emit", "emit")
            if emitter is None:
                await post_telemetry_event(batch, self.config)
            else:
                result = emitter(batch)
                if inspect.isawaitable(result):
                    await result
        except Exception as error:
            on_error = _config_value(self.config, "on_error", "onError")
            if callable(on_error):
                try:
                    result = on_error(error, batch)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass

    async def _drain(self) -> None:
        while self._pending:
            items = [
                self._pending.popleft()
                for _ in range(min(PRIVACY_QUEUE_BATCH_SIZE, len(self._pending)))
            ]
            events: list[AnalyticsIngestEvent] = []
            for finalize in items:
                try:
                    finalized = finalize()
                    if inspect.isawaitable(finalized):
                        finalized = await finalized
                    if finalized:
                        events.extend(finalized)
                except Exception as error:
                    self._warn(f"privacy queue candidate failed and was dropped: {error}")
            if events:
                await self._emit({"schema_version": SCHEMA_VERSION, "events": events})  # type: ignore[typeddict-item]

    def _start(self) -> asyncio.Task[None]:
        if self._running is not None:
            return self._running

        async def run() -> None:
            try:
                await self._drain()
            finally:
                self._running = None
                if self._pending and self._enabled():
                    self._schedule_background()

        self._running = asyncio.get_running_loop().create_task(run())
        return self._running

    def _schedule_background(self) -> None:
        work = self._start()
        schedule = self._schedule_hook()
        if schedule is not None:
            try:
                schedule(work)
            except Exception as error:
                self._warn(f"schedule hook threw; background work remains active: {error}")

    async def enqueue(self, finalize: PrivacyQueueFinalizer) -> None:
        if not self._enabled():
            return
        if len(self._pending) >= PRIVACY_QUEUE_CAPACITY:
            self._pending.popleft()
            self._dropped += 1
            if not self._warned_dropped:
                self._warned_dropped = True
                self._warn(
                    f"privacy queue overflow; dropped {self._dropped} oldest candidate(s)"
                )
        self._pending.append(finalize)
        if self._delivery() == "await":
            await self.flush()
        elif self._running is None:
            self._schedule_background()

    async def flush(self) -> None:
        if not self._enabled():
            return
        while self._running is not None or self._pending:
            work = self._running or self._start()
            await asyncio.shield(work)


def create_privacy_queue(
    config: AnalyticsConfig | None = None,
) -> PrivacyQueue:
    return PrivacyQueue(config)
