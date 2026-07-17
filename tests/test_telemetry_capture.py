from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any

from armature_mcp_analytics import (
    AnalyticsIngestBatch,
    BASE64_REMOVED_PLACEHOLDER,
    REDACTION_FAILED_PLACEHOLDER,
    apply_telemetry_field_map,
    build_tool_call_event,
    create_analytics_recorder,
    extract_telemetry_arguments,
    plan_tool_telemetry,
    sanitize_value,
    schema_declares_telemetry,
)

_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "telemetry_contract_vectors.json").read_text()
)


class TelemetryContractVectorsTest(unittest.TestCase):
    def test_extraction_vectors(self) -> None:
        for vector in _VECTORS["extraction"]:
            with self.subTest(vector["name"]):
                args, telemetry = extract_telemetry_arguments(vector["args"], vector["mode"])
                self.assertEqual(args, vector["expect_args"])
                self.assertEqual(telemetry, vector["expect_telemetry"])

    def test_sanitization_vectors(self) -> None:
        for vector in _VECTORS["sanitization"]:
            with self.subTest(vector["name"]):
                self.assertEqual(sanitize_value(vector["value"]), vector["expect"])


class CapturePlanTest(unittest.TestCase):
    def test_capture_off_is_scrub_and_leaves_everything_untouched(self) -> None:
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        plan = plan_tool_telemetry("search", schema, {"armature": {"capture_telemetry": False}})
        self.assertEqual(plan.mode, "scrub")
        self.assertIs(plan.input_schema, schema)
        self.assertEqual(plan.apply_description("Find things."), "Find things.")
        self.assertIsNone(plan.apply_description(None))

    def test_default_is_injected_with_hint(self) -> None:
        plan = plan_tool_telemetry("search", {"type": "object", "properties": {}})
        self.assertEqual(plan.mode, "injected")
        self.assertIn("telemetry", plan.input_schema["properties"])
        self.assertIn("telemetry.user_intent", plan.apply_description("Find things."))

    def test_owned_schema_is_never_decorated(self) -> None:
        owned = {
            "type": "object",
            "properties": {"telemetry": {"type": "string", "description": "customer field"}},
        }
        self.assertTrue(schema_declares_telemetry(owned))
        plan = plan_tool_telemetry("customer-tool", owned)
        self.assertEqual(plan.mode, "owned")
        self.assertIs(plan.input_schema, owned)
        self.assertEqual(plan.apply_description("Mine."), "Mine.")

    def test_strict_with_capture_off_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "capture_telemetry is False"):
            create_analytics_recorder(
                {
                    "armature": {"capture_telemetry": False},
                    "telemetry": {"user_intent": "required"},
                }
            )


class CaptureRecorderTest(unittest.TestCase):
    def _record(self, config_armature: dict[str, Any], **event: Any) -> AnalyticsIngestBatch:
        batches: list[AnalyticsIngestBatch] = []

        def emit(batch: AnalyticsIngestBatch) -> None:
            batches.append(batch)

        recorder = create_analytics_recorder(
            {"armature": {"delivery": "await", "emit": emit, **config_armature}}
        )
        asyncio.run(recorder.record_tool_call(status="ok", **event))
        return batches[0]

    def test_capture_off_drops_telemetry_even_from_direct_callers(self) -> None:
        batch = self._record(
            {"capture_telemetry": False},
            name="search",
            args={"q": "x"},
            telemetry={"user_intent": "should never ship"},
            result={"ok": True},
        )
        event = next(e for e in batch["events"] if e["kind"] == "tool_call")
        for key in ("user_intent", "agent_thinking", "user_frustration", "user_turn", "intent", "context"):
            self.assertIsNone(event["metadata"][key], key)

    def test_direct_record_for_registered_owned_tool_drops_telemetry(self) -> None:
        batches: list[AnalyticsIngestBatch] = []

        def emit(batch: AnalyticsIngestBatch) -> None:
            batches.append(batch)

        recorder = create_analytics_recorder({"armature": {"delivery": "await", "emit": emit}})
        recorder.tool(
            {
                "name": "owned-direct",
                "inputSchema": {"type": "object", "properties": {"telemetry": {"type": "string"}}},
            },
            lambda args, context: {"ok": True},
        )
        # An adapter bypassing dispatch() must not export telemetry for a tool
        # the customer owns — the choke point consults the registered mode.
        asyncio.run(
            recorder.record_tool_call(
                name="owned-direct",
                args={"telemetry": "customer value"},
                telemetry={"user_intent": "adapter-supplied"},
                status="ok",
                result={"ok": True},
            )
        )
        event = next(e for e in batches[0]["events"] if e["kind"] == "tool_call")
        self.assertIsNone(event["metadata"]["user_intent"])

    def test_field_map_exports_customer_field(self) -> None:
        batch = self._record(
            {"telemetry_field_map": {"user_intent": "purpose"}},
            name="customer-tool",
            args={"purpose": "book a flight"},
            result={"ok": True},
        )
        event = next(e for e in batch["events"] if e["kind"] == "tool_call")
        self.assertEqual(event["metadata"]["user_intent"], "book a flight")
        self.assertIsNone(event["metadata"]["agent_thinking"])


class SyncWrapperOwnershipTest(unittest.TestCase):
    def test_sync_wrapper_preserves_owned_telemetry_argument(self) -> None:
        # Regression: the sync wrapper must thread the resolved telemetry mode
        # exactly like the async wrapper, or a synchronous tool that owns its
        # telemetry field has it stripped and exported as analytics data.
        from armature_mcp_analytics.server import _wrap_handler

        batches: list[AnalyticsIngestBatch] = []

        def emit(batch: AnalyticsIngestBatch) -> None:
            batches.append(batch)

        recorder = create_analytics_recorder({"armature": {"delivery": "await", "emit": emit}})
        seen: list[Any] = []

        def owned_tool(**kwargs: Any) -> dict[str, Any]:
            seen.append(kwargs)
            return {"ok": True}

        wrapped = _wrap_handler(recorder, "owned-sync", owned_tool, "owned")
        asyncio.run(wrapped(telemetry="customer value", q="x"))

        self.assertEqual(seen, [{"telemetry": "customer value", "q": "x"}])
        event = next(e for e in batches[0]["events"] if e["kind"] == "tool_call")
        self.assertIsNone(event["metadata"]["user_intent"])


class FieldMapTest(unittest.TestCase):
    def test_integral_float_turn_matches_normalizer_contract(self) -> None:
        self.assertEqual(
            apply_telemetry_field_map(None, {"turn": 2.0}, {"user_turn": "turn"}),
            {"user_turn": 2},
        )

    def test_explicit_telemetry_wins_and_types_validated(self) -> None:
        self.assertEqual(
            apply_telemetry_field_map(
                {"user_intent": "explicit"},
                {"purpose": "mapped", "turn": 2, "mood": "high"},
                {"user_intent": "purpose", "user_turn": "turn", "user_frustration": "mood"},
            ),
            {"user_intent": "explicit", "user_turn": 2, "user_frustration": "high"},
        )
        self.assertIsNone(
            apply_telemetry_field_map(
                None,
                {"turn": "not a number", "mood": "irate"},
                {"user_turn": "turn", "user_frustration": "mood"},
            )
        )
        self.assertEqual(
            apply_telemetry_field_map(
                {
                    "intent": "legacy explicit",
                    "context": "legacy context",
                    "frustration_level": "medium",
                },
                {"purpose": "mapped", "thinking": "mapped thinking", "mood": "high"},
                {
                    "user_intent": "purpose",
                    "agent_thinking": "thinking",
                    "user_frustration": "mood",
                },
            ),
            {
                "intent": "legacy explicit",
                "context": "legacy context",
                "frustration_level": "medium",
            },
        )


class RedactionPipelineTest(unittest.TestCase):
    def test_previews_sanitize_and_redact(self) -> None:
        def redact(value: Any) -> Any:
            return json.loads(json.dumps(value).replace("secret-token-12345", "[redacted]"))

        event = build_tool_call_event(
            tool_name="upload",
            telemetry=None,
            input={
                "file": {"type": "image", "data": "QUFB" * 200, "mimeType": "image/png"},
                "note": "secret-token-12345",
            },
            output={"stored": "QUFB" * 200},
            status="ok",
            duration_ms=5,
            error_message=None,
            actor_id="actor",
            session_id=None,
            request_id="req",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            redact=redact,
        )
        self.assertIn("[binary removed]", event["metadata"]["input_preview"])
        self.assertNotIn("secret-token-12345", event["metadata"]["input_preview"])
        self.assertIn("[redacted]", event["metadata"]["input_preview"])
        self.assertIn("[redacted]", event["script_source"])
        self.assertIn(BASE64_REMOVED_PLACEHOLDER, event["result_preview"])

    def test_throwing_redact_hook_fails_closed(self) -> None:
        def redact(value: Any) -> Any:
            raise RuntimeError("boom")

        event = build_tool_call_event(
            tool_name="upload",
            telemetry={"user_intent": "quotes the user"},
            input={"secret": "leak me not"},
            output={"alsoSecret": True},
            status="error",
            duration_ms=5,
            error_message="failed with leak me not",
            actor_id="actor",
            session_id=None,
            request_id="req",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            redact=redact,
        )
        self.assertEqual(event["metadata"]["input_preview"], json.dumps(REDACTION_FAILED_PLACEHOLDER))
        self.assertEqual(event["result_preview"], json.dumps(REDACTION_FAILED_PLACEHOLDER))
        self.assertEqual(event["error"], REDACTION_FAILED_PLACEHOLDER)
        self.assertIsNone(event["metadata"]["user_intent"])
        self.assertNotIn("leak me not", event["script_source"])


if __name__ == "__main__":
    unittest.main()
