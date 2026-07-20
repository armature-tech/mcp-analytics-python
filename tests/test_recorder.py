from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from armature_mcp_analytics import create_analytics_recorder, events
from armature_mcp_analytics.emit import post_telemetry_event


class RecorderTests(unittest.TestCase):
    def test_actor_identifier_is_sent_verbatim_and_emitted_only_on_change(self) -> None:
        batches = []
        actor = {"identifier": "Ada <ada@example.com>"}
        calls = 0

        def identifier(_input):
            nonlocal calls
            calls += 1
            return actor["identifier"]

        recorder = create_analytics_recorder({
            "armature": {
                "delivery": "await",
                "actor_identifier": identifier,
                "emit": batches.append,
            }
        })

        async def run() -> None:
            for request_id in ("one", "two"):
                await recorder.record_tool_call(
                    name="ping", status="ok", session_id="identifier-session", request_id=request_id
                )
            actor["identifier"] = "anything-at-all@example.com"
            await recorder.record_tool_call(
                name="ping", status="ok", session_id="identifier-session", request_id="three"
            )

        asyncio.run(run())
        identity_events = [
            event for batch in batches for event in batch["events"]
            if event["kind"] == "actor_identity"
        ]
        self.assertEqual(calls, 3)
        self.assertEqual(len(identity_events), 2)
        self.assertEqual(identity_events[0]["metadata"]["identifier"], "Ada <ada@example.com>")
        self.assertEqual(
            identity_events[0]["actor_id"],
            events.build_actor_id(actor_seed="Ada <ada@example.com>"),
        )
        self.assertEqual(
            identity_events[1]["actor_id"],
            events.build_actor_id(actor_seed="anything-at-all@example.com"),
        )

    def test_actor_identity_is_omitted_without_actor_identifier(self) -> None:
        batches = []
        recorder = create_analytics_recorder({
            "armature": {
                "delivery": "await",
                "actor_id": "actor-without-identifier",
                "emit": batches.append,
            }
        })
        asyncio.run(recorder.record_tool_call(name="ping", status="ok"))
        self.assertFalse(any(
            event["kind"] == "actor_identity"
            for batch in batches for event in batch["events"]
        ))

    def test_default_emitter_uses_cloudflare_safe_headers(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 202

        with mock.patch(
            "armature_mcp_analytics.emit.urllib.request.urlopen",
            return_value=Response(),
        ) as urlopen:
            asyncio.run(post_telemetry_event(
                {"schema_version": 1, "events": []},
                {"armature": {"api_key": "ami_test", "delivery": "await"}},
            ))

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer ami_test")
        self.assertEqual(request.get_header("User-agent"), "armature-mcp-analytics-python")

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
                {"customer_id": "cus_123", "telemetry": {"user_intent": "check account", "user_turn": 1}},
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
        self.assertEqual(events[1]["metadata"]["user_intent"], "check account")
        self.assertNotIn("user_turn", events[1]["metadata"])
        # Legacy mirror: a not-yet-updated ingest keeps reading `intent`.
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
                {"name": "call_upstream", "args": {"telemetry": {"user_intent": "fetch page"}}},
                lambda _args: {
                    "isError": True,
                    "content": [{"type": "text", "text": "upstream 404"}],
                },
            )

        asyncio.run(run())

        events = [event for batch in batches for event in batch["events"]]
        tool_call = next(event for event in events if event["kind"] == "tool_call")
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
                    {"name": "cancelled_tool", "args": {"telemetry": {"user_intent": "stop work"}}},
                    cancelling_handler,
                )
            await recorder.flush()

        asyncio.run(run())

        events = [event for batch in batches for event in batch["events"]]
        tool_call = next(event for event in events if event["kind"] == "tool_call")
        self.assertFalse(tool_call["ok"])
        self.assertEqual(tool_call["metadata"]["tool_name"], "cancelled_tool")
        self.assertEqual(tool_call["metadata"]["user_intent"], "stop work")

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


class StdioSessionFallbackTests(unittest.TestCase):
    """Regression suite for the "two `claude -p` conversations merged into one
    Armature activity" bug. stdio transports never carry a session id and have
    no HTTP headers, so every event went out with `session_id_hint: None` and
    no `session_init` was ever emitted. Ingest groups null-hint events into a
    per-actor DAILY bucket, so two distinct same-day CLI sessions from the same
    user became indistinguishable. The fix: a stdio server process serves
    exactly one connection, so the recorder falls back to a process-scoped
    session id whenever a request carries no session signal and no headers."""

    def setUp(self) -> None:
        events._reset_process_scoped_session_id_for_tests()

    def tearDown(self) -> None:
        events._reset_process_scoped_session_id_for_tests()

    def _recorder(self, batches: list) -> object:
        return create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "same-cli-user",
                    "emit": batches.append,
                }
            }
        )

    def test_stdio_processes_get_distinct_stable_session_ids(self) -> None:
        # First `claude -p` run.
        first_batches: list = []
        first = self._recorder(first_batches)

        async def run_first() -> None:
            await first.record_tool_call(name="lookup_customer", status="ok", args={})
            await first.record_tool_call(name="lookup_customer", status="ok", args={})

        asyncio.run(run_first())

        # Second run: a fresh process, simulated by resetting the singleton.
        events._reset_process_scoped_session_id_for_tests()
        second_batches: list = []
        second = self._recorder(second_batches)

        async def run_second() -> None:
            await second.record_tool_call(name="lookup_customer", status="ok", args={})

        asyncio.run(run_second())

        first_events = [event for batch in first_batches for event in batch["events"]]
        second_events = [event for batch in second_batches for event in batch["events"]]

        # Before the fix every one of these hints was None — the exact payload
        # observed in production — and ingest merged both runs into one bucket.
        for event in first_events + second_events:
            self.assertIsNotNone(event["session_id_hint"], f"{event['kind']} must carry a session id on stdio")

        # Stable within one process, session_init emitted exactly once.
        self.assertEqual(len({event["session_id_hint"] for event in first_events}), 1)
        self.assertEqual(sum(1 for event in first_events if event["kind"] == "session_init"), 1)

        # Distinct across processes: the runs can no longer collapse together.
        self.assertNotEqual(first_events[0]["session_id_hint"], second_events[0]["session_id_hint"])

    def test_http_shaped_requests_keep_null_hint_and_no_session_init(self) -> None:
        batches: list = []
        recorder = self._recorder(batches)

        async def run() -> None:
            # A stateless HTTP invocation that did not echo Mcp-Session-Id:
            # headers exist, so the process-scoped fallback must NOT kick in —
            # many sessions share one long-lived HTTP server process.
            await recorder.record_tool_call(
                name="lookup_customer",
                status="ok",
                args={},
                extra={"requestInfo": {"headers": {"user-agent": "python-httpx"}}},
            )

        asyncio.run(run())

        recorded = [event for batch in batches for event in batch["events"]]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["kind"], "tool_call")
        self.assertIsNone(recorded[0]["session_id_hint"])

    def test_headers_passed_directly_resolve_mcp_session_id_before_fallback(self) -> None:
        batches: list = []
        recorder = self._recorder(batches)

        async def run() -> None:
            await recorder.record_tool_call(
                name="lookup_customer",
                status="ok",
                args={},
                headers={"Mcp-Session-Id": "header-session-77"},
            )

        asyncio.run(run())

        recorded = [event for batch in batches for event in batch["events"]]
        tool_call = next(event for event in recorded if event["kind"] == "tool_call")
        self.assertEqual(tool_call["session_id_hint"], "header-session-77")

    def test_empty_headers_still_count_as_http_no_fallback(self) -> None:
        batches: list = []
        recorder = self._recorder(batches)

        async def run() -> None:
            # A pathological HTTP request whose headers were all stripped
            # upstream: present-but-empty must NOT be conflated with "no HTTP
            # request" or every session on the process would merge.
            await recorder.record_tool_call(
                name="lookup_customer", status="ok", args={}, headers={}
            )

        asyncio.run(run())

        recorded = [event for batch in batches for event in batch["events"]]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["kind"], "tool_call")
        self.assertIsNone(recorded[0]["session_id_hint"])

    def test_explicit_session_id_wins_over_fallback(self) -> None:
        batches: list = []
        recorder = self._recorder(batches)

        async def run() -> None:
            await recorder.record_tool_call(
                name="lookup_customer", status="ok", args={}, session_id="session-explicit"
            )

        asyncio.run(run())

        recorded = [event for batch in batches for event in batch["events"]]
        for event in recorded:
            self.assertEqual(event["session_id_hint"], "session-explicit")


if __name__ == "__main__":
    unittest.main()
