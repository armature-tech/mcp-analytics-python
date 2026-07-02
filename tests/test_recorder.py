from __future__ import annotations

import asyncio
import unittest

from armature_mcp_analytics import create_analytics_recorder


class RecorderTests(unittest.TestCase):
    def test_dispatch_strips_telemetry_and_emits_tool_call(self) -> None:
        batches = []
        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "actor-one",
                    "emit": batches.append,
                }
            }
        )
        seen = {}

        async def run() -> None:
            recorder.tool(
                {"name": "lookup_customer", "inputSchema": {"type": "object", "properties": {}}},
                lambda args, _context: seen.update(args) or {"ok": True},
            )
            result = await recorder.dispatch(
                "lookup_customer",
                {"customer_id": "cus_123", "telemetry": {"intent": "check account"}},
                {"session_id": "session-1"},
            )
            self.assertEqual(result, {"ok": True})
            await recorder.flush()

        asyncio.run(run())

        self.assertEqual(seen, {"customer_id": "cus_123"})
        events = [event for batch in batches for event in batch["events"]]
        self.assertEqual(events[0]["kind"], "session_init")
        self.assertEqual(events[1]["kind"], "tool_call")
        self.assertEqual(events[1]["metadata"]["tool_name"], "lookup_customer")
        self.assertEqual(events[1]["metadata"]["intent"], "check account")

    def test_returned_is_error_records_failed_tool_call(self) -> None:
        batches = []
        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "actor-two",
                    "emit": batches.append,
                }
            }
        )

        async def run() -> None:
            await recorder.instrument_tool_call(
                {"name": "call_upstream", "args": {"telemetry": {"intent": "fetch page"}}},
                lambda _args: {
                    "isError": True,
                    "content": [{"type": "text", "text": "upstream 404"}],
                },
            )

        asyncio.run(run())

        tool_call = batches[0]["events"][0]
        self.assertFalse(tool_call["ok"])
        self.assertEqual(tool_call["error"], "upstream 404")

    def test_missing_api_key_noops_without_emit(self) -> None:
        recorder = create_analytics_recorder({"armature": {"delivery": "await"}})

        async def run() -> None:
            await recorder.record_tool_call(name="ping", status="ok", args={})
            await recorder.flush()

        asyncio.run(run())

    def test_cancelled_tool_call_propagates_and_records_error(self) -> None:
        batches = []
        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "cancel-actor",
                    "emit": batches.append,
                }
            }
        )

        async def cancelling_handler(_args):
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

        async def run() -> None:
            with self.assertRaises(asyncio.CancelledError):
                await recorder.instrument_tool_call(
                    {"name": "cancelled_tool", "args": {"telemetry": {"intent": "stop work"}}},
                    cancelling_handler,
                )
            await recorder.flush()

        asyncio.run(run())

        tool_call = batches[0]["events"][0]
        self.assertFalse(tool_call["ok"])
        self.assertEqual(tool_call["metadata"]["tool_name"], "cancelled_tool")
        self.assertEqual(tool_call["metadata"]["intent"], "stop work")

    def test_success_result_survives_telemetry_recording_failure(self) -> None:
        def failing_actor_id(_input):
            raise RuntimeError("actor resolver failed")

        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": failing_actor_id,
                    "emit": lambda _batch: None,
                }
            }
        )

        async def run() -> dict:
            return await recorder.instrument_tool_call(
                {"name": "successful_tool", "args": {}},
                lambda _args: {"ok": True},
            )

        self.assertEqual(asyncio.run(run()), {"ok": True})

    def test_success_result_survives_cancellation_during_recording(self) -> None:
        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "actor-three",
                    "emit": lambda _batch: None,
                }
            }
        )

        async def cancelling_record_tool_call(*_args, **_kwargs) -> None:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

        recorder.record_tool_call = cancelling_record_tool_call

        async def run() -> dict:
            return await recorder.instrument_tool_call(
                {"name": "successful_tool", "args": {}},
                lambda _args: {"ok": True},
            )

        self.assertEqual(asyncio.run(run()), {"ok": True})

    def test_tool_error_survives_telemetry_recording_failure(self) -> None:
        def failing_actor_id(_input):
            raise RuntimeError("actor resolver failed")

        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": failing_actor_id,
                    "emit": lambda _batch: None,
                }
            }
        )

        async def run() -> None:
            await recorder.instrument_tool_call(
                {"name": "failing_tool", "args": {}},
                lambda _args: (_ for _ in ()).throw(ValueError("tool failed")),
            )

        with self.assertRaisesRegex(ValueError, "tool failed"):
            asyncio.run(run())

    def test_tool_error_survives_cancellation_during_error_recording(self) -> None:
        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "actor-three",
                    "emit": lambda _batch: None,
                }
            }
        )

        async def cancelling_record_tool_call(*_args, **_kwargs) -> None:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

        recorder.record_tool_call = cancelling_record_tool_call

        async def run() -> None:
            await recorder.instrument_tool_call(
                {"name": "failing_tool", "args": {}},
                lambda _args: (_ for _ in ()).throw(ValueError("tool failed")),
            )

        with self.assertRaisesRegex(ValueError, "tool failed"):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
