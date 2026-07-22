from __future__ import annotations

import asyncio
import unittest

from armature_mcp_analytics.emit import create_flushable_emitter


class EmitTests(unittest.TestCase):
    def test_flush_waits_for_background_tasks(self) -> None:
        emitted = []

        async def emit(batch) -> None:
            await asyncio.sleep(0.01)
            emitted.append(batch)

        emitter = create_flushable_emitter({"armature": {"emit": emit}})
        batch = {"schema_version": 1, "events": []}

        async def run() -> None:
            await emitter.emit_batch(batch)
            self.assertEqual(emitted, [])
            await emitter.flush()

        asyncio.run(run())

        self.assertEqual(emitted, [batch])

    def test_on_error_callback_failures_do_not_escape_await_delivery(self) -> None:
        async def emit(_batch) -> None:
            raise RuntimeError("network failed")

        def on_error(_error, _batch) -> None:
            raise RuntimeError("callback failed")

        emitter = create_flushable_emitter(
            {"armature": {"delivery": "await", "emit": emit, "on_error": on_error}}
        )

        asyncio.run(emitter.emit_batch({"schema_version": 1, "events": []}))

    def test_async_on_error_callback_is_awaited(self) -> None:
        observed = []

        async def emit(_batch) -> None:
            raise RuntimeError("network failed")

        async def on_error(error, batch) -> None:
            await asyncio.sleep(0)
            observed.append((str(error), batch))

        batch = {"schema_version": 1, "events": []}
        emitter = create_flushable_emitter(
            {"armature": {"delivery": "await", "emit": emit, "on_error": on_error}}
        )

        asyncio.run(emitter.emit_batch(batch))

        self.assertEqual(observed, [("network failed", batch)])


if __name__ == "__main__":
    unittest.main()
