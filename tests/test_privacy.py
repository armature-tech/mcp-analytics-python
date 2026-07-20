from __future__ import annotations

import asyncio
import json
import unittest
import warnings
from pathlib import Path
from typing import Any

from armature_mcp_analytics import (
    PRIVACY_QUEUE_CAPACITY,
    REDACTION_FAILED_PLACEHOLDER,
    create_analytics_recorder,
    create_privacy_queue,
    prepare_for_preview,
    sanitize_value,
)
from armature_mcp_analytics.utils import MAX_PREVIEW_BYTES, MAX_SOURCE_BYTES, truncate_utf8


FIXTURE = Path(__file__).parent / "fixtures" / "telemetry_contract_vectors.json"


def _event(identifier: int) -> dict[str, Any]:
    return {
        "event_id": str(identifier),
        "kind": "tool_call",
        "actor_id": "actor",
        "session_id_hint": None,
        "started_at": "1970-01-01T00:00:00.000Z",
        "finished_at": "1970-01-01T00:00:00.000Z",
        "duration_ms": 0,
        "ok": True,
        "error": None,
        "metadata": {},
        "script_source": None,
        "script_source_truncated": False,
        "result_preview": None,
        "result_truncated": False,
        "calls": [],
        "logs": [],
        "search_calls": [],
    }


class SanitizationTests(unittest.TestCase):
    def test_cross_sdk_sanitization_and_secret_vectors(self) -> None:
        vectors = json.loads(FIXTURE.read_text())
        for vector in vectors["sanitization"]:
            self.assertEqual(
                sanitize_value(vector["value"]), vector["expect"], vector["name"]
            )
        for vector in vectors["secret_redaction"]:
            value = (
                "".join(vector["value_parts"])
                if "value_parts" in vector
                else vector["value"]
            )
            self.assertEqual(
                prepare_for_preview(value), vector["expect"], vector["name"]
            )

    def test_secret_redaction_can_be_disabled_without_disabling_sanitization(self) -> None:
        self.assertEqual(
            prepare_for_preview(
                {
                    "token": "sk-proj-AbCdEfGhIjKlMnOpQrStUv123456",
                    "blob": "QUFB",
                },
                redact_secrets=False,
            ),
            {
                "token": "sk-proj-AbCdEfGhIjKlMnOpQrStUv123456",
                "blob": "[binary removed]",
            },
        )

    def test_shared_values_are_preserved_and_cycles_are_cut(self) -> None:
        shared = {"blob": "QUFBQQ==", "note": "keep"}
        self.assertEqual(
            sanitize_value([shared, shared]),
            [
                {"blob": "[binary removed]", "note": "keep"},
                {"blob": "[binary removed]", "note": "keep"},
            ],
        )
        cyclic: dict[str, Any] = {"note": "keep"}
        cyclic["self"] = cyclic
        self.assertEqual(
            sanitize_value(cyclic), {"note": "keep", "self": "[circular]"}
        )

    def test_bounded_sanitization_preserves_serialization_horizon(self) -> None:
        payload = {
            "prefix": "kept",
            "body": "not-base64 content " * 160_000,
            "tail": "never reached",
        }
        bounded_text = json.dumps(sanitize_value(payload), separators=(",", ":"))
        unbounded_text = json.dumps(payload, separators=(",", ":"))
        self.assertEqual(bounded_text[:32_768], unbounded_text[:32_768])
        self.assertEqual(
            truncate_utf8(bounded_text, MAX_PREVIEW_BYTES),
            truncate_utf8(unbounded_text, MAX_PREVIEW_BYTES),
        )
        self.assertEqual(
            truncate_utf8(f"MCP tool call: huge\n\nInput:\n{bounded_text}", MAX_SOURCE_BYTES),
            truncate_utf8(f"MCP tool call: huge\n\nInput:\n{unbounded_text}", MAX_SOURCE_BYTES),
        )


class PrivacyQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_and_natural_batching_behind_slow_post(self) -> None:
        gate = asyncio.Event()
        batches: list[list[str]] = []
        sends = 0

        async def emit(batch) -> None:
            nonlocal sends
            batches.append([item["event_id"] for item in batch["events"]])
            sends += 1
            if sends == 1:
                await gate.wait()

        queue = create_privacy_queue({"armature": {"emit": emit}})
        await queue.enqueue(lambda: [_event(0)])
        await asyncio.sleep(0)
        await queue.enqueue(lambda: [_event(1)])
        await queue.enqueue(lambda: [_event(2)])
        gate.set()
        await queue.flush()
        self.assertEqual(batches, [["0"], ["1", "2"]])

    async def test_overflow_drops_oldest_and_flush_drains_pipeline(self) -> None:
        identifiers: list[str] = []
        queue = create_privacy_queue(
            {
                "armature": {
                    "emit": lambda batch: identifiers.extend(
                        item["event_id"] for item in batch["events"]
                    )
                }
            }
        )
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always")
            for identifier in range(PRIVACY_QUEUE_CAPACITY + 1):
                await queue.enqueue(lambda identifier=identifier: [_event(identifier)])
            await queue.flush()
        self.assertEqual(len(identifiers), PRIVACY_QUEUE_CAPACITY)
        self.assertEqual(identifiers[0], "1")
        self.assertEqual(identifiers[-1], str(PRIVACY_QUEUE_CAPACITY))
        self.assertEqual(
            sum("overflow" in str(warning.message) for warning in observed), 1
        )

    async def test_await_mode_waits_for_finalization_and_export(self) -> None:
        finalize_gate = asyncio.Event()
        finalize_started = asyncio.Event()
        emit_gate = asyncio.Event()
        steps: list[str] = []

        async def emit(_batch) -> None:
            steps.append("emit")
            await emit_gate.wait()

        queue = create_privacy_queue(
            {"armature": {"delivery": "await", "emit": emit}}
        )

        async def finalize():
            steps.append("finalize")
            finalize_started.set()
            await finalize_gate.wait()
            return [_event(1)]

        work = asyncio.create_task(queue.enqueue(finalize))
        await finalize_started.wait()
        self.assertEqual(steps, ["finalize"])
        finalize_gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(steps, ["finalize", "emit"])
        emit_gate.set()
        await work

    async def test_schedule_receives_background_work(self) -> None:
        scheduled = []
        queue = create_privacy_queue(
            {
                "armature": {
                    "schedule": scheduled.append,
                    "emit": lambda _batch: None,
                }
            }
        )
        await queue.enqueue(lambda: [_event(1)])
        self.assertEqual(len(scheduled), 1)
        await scheduled[0]

    async def test_redact_event_mutates_drops_and_fails_closed(self) -> None:
        batches = []

        async def redact_event(candidate):
            if candidate["tool_name"] == "drop":
                return None
            if candidate["tool_name"] == "throw":
                raise RuntimeError("hook failed")
            return {**candidate, "input": {"safe": True}, "tool_name": "mutated"}

        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "actor",
                    "redact_event": redact_event,
                    "emit": batches.append,
                }
            }
        )
        await recorder.record_tool_call(
            name="keep", args={"password": "secret"}, status="ok"
        )
        await recorder.record_tool_call(
            name="drop", args={"secret": "secret"}, status="ok"
        )
        await recorder.record_tool_call(
            name="throw", args={"secret": "leak"}, status="error"
        )
        tool_events = [
            event
            for batch in batches
            for event in batch["events"]
            if event["kind"] == "tool_call"
        ]
        self.assertEqual(len(tool_events), 2)
        self.assertEqual(tool_events[0]["metadata"]["tool_name"], "mutated")
        self.assertEqual(
            json.loads(tool_events[0]["metadata"]["input_preview"]), {"safe": True}
        )
        self.assertEqual(
            tool_events[1]["metadata"]["input_preview"],
            json.dumps(REDACTION_FAILED_PLACEHOLDER, separators=(",", ":")),
        )
        self.assertEqual(tool_events[1]["error"], REDACTION_FAILED_PLACEHOLDER)
        self.assertNotIn("leak", json.dumps(tool_events))

    async def test_legacy_redact_runs_after_builtin_protection(self) -> None:
        batches = []
        observed = []

        def redact(value):
            observed.append(value)
            if isinstance(value, dict) and value.get("visible") == "remove-me":
                return {"visible": "legacy-redacted"}
            return value

        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "actor",
                    "redact": redact,
                    "emit": batches.append,
                }
            }
        )
        await recorder.record_tool_call(
            name="legacy",
            args={
                "visible": "remove-me",
                "token": "sk-proj-AbCdEfGhIjKlMnOpQrStUv123456",
            },
            telemetry={"user_intent": "use AKIAIOSFODNN7EXAMPLE"},
            status="error",
            error="Bearer abcdef1234567890abcdef",
        )
        tool_event = next(
            event
            for batch in batches
            for event in batch["events"]
            if event["kind"] == "tool_call"
        )
        self.assertEqual(
            json.loads(tool_event["metadata"]["input_preview"]),
            {"visible": "legacy-redacted"},
        )
        self.assertEqual(tool_event["error"], "Bearer [redacted:bearer]")
        self.assertEqual(
            tool_event["metadata"]["user_intent"],
            "use [redacted:aws-access-key-id]",
        )
        self.assertTrue(
            any(
                isinstance(value, dict)
                and value.get("token") == "[redacted:sensitive-field]"
                for value in observed
            )
        )

    async def test_legacy_redact_failures_fail_closed_per_field(self) -> None:
        batches = []

        def redact(_value):
            raise RuntimeError("redaction failed")

        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "actor",
                    "redact": redact,
                    "emit": batches.append,
                }
            }
        )
        await recorder.record_tool_call(
            name="legacy-throw",
            args={"secret": "leak-input"},
            result={"secret": "leak-output"},
            telemetry={"user_intent": "leak-telemetry"},
            status="error",
            error="leak-error",
        )
        tool_event = next(
            event
            for batch in batches
            for event in batch["events"]
            if event["kind"] == "tool_call"
        )
        self.assertEqual(
            tool_event["metadata"]["input_preview"],
            json.dumps(REDACTION_FAILED_PLACEHOLDER),
        )
        self.assertEqual(tool_event["result_preview"], json.dumps(REDACTION_FAILED_PLACEHOLDER))
        self.assertEqual(tool_event["error"], REDACTION_FAILED_PLACEHOLDER)
        self.assertIsNone(tool_event["metadata"]["user_intent"])
        self.assertNotIn("leak-", json.dumps(tool_event))

    async def test_background_recorder_returns_before_async_hook(self) -> None:
        gate = asyncio.Event()
        emitted = False

        async def redact_event(candidate):
            await gate.wait()
            return candidate

        def emit(_batch) -> None:
            nonlocal emitted
            emitted = True

        recorder = create_analytics_recorder(
            {"armature": {"redact_event": redact_event, "emit": emit}}
        )
        await recorder.record_tool_call(name="background", status="ok")
        self.assertFalse(emitted)
        gate.set()
        await recorder.flush()
        self.assertTrue(emitted)


if __name__ == "__main__":
    unittest.main()
