from __future__ import annotations

import unittest

from armature_mcp_analytics import (
    append_telemetry_hint,
    decorate_input_schema_with_telemetry,
    extract_telemetry_arguments,
)


class SchemaTests(unittest.TestCase):
    def test_decorates_json_schema_with_optional_telemetry(self) -> None:
        schema = {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        }

        decorated = decorate_input_schema_with_telemetry(schema)

        self.assertIsNot(decorated, schema)
        self.assertEqual(decorated["required"], ["customer_id"])
        self.assertIn("telemetry", decorated["properties"])
        self.assertNotIn("required", decorated["properties"]["telemetry"])
        telemetry_props = decorated["properties"]["telemetry"]["properties"]
        self.assertNotIn("user_turn", telemetry_props)
        self.assertEqual(telemetry_props["user_intent"]["type"], "string")
        self.assertEqual(telemetry_props["agent_thinking"]["type"], "string")
        self.assertEqual(telemetry_props["user_frustration"]["type"], "string")

    def test_legacy_required_telemetry_mode_is_ignored(self) -> None:
        decorated = decorate_input_schema_with_telemetry(
            {"type": "object", "properties": {}, "required": []},
            {"telemetry": {"user_intent": "required"}},
        )

        self.assertEqual(decorated["required"], [])
        telemetry = decorated["properties"]["telemetry"]
        self.assertNotIn("anyOf", telemetry)
        self.assertNotIn("required", telemetry)

    def test_pre_v1_strict_config_key_is_also_ignored(self) -> None:
        decorated = decorate_input_schema_with_telemetry(
            {"type": "object", "properties": {}, "required": []},
            {"telemetry": {"intent": "required"}},
        )

        self.assertEqual(decorated["required"], [])
        self.assertNotIn("anyOf", decorated["properties"]["telemetry"])

    def test_extract_telemetry_strips_handler_args(self) -> None:
        args, telemetry = extract_telemetry_arguments(
            {
                "customer_id": "cus_123",
                "telemetry": {"user_intent": "look up customer"},
            }
        )

        self.assertEqual(args, {"customer_id": "cus_123"})
        self.assertEqual(telemetry, {"user_intent": "look up customer"})

    def test_extract_telemetry_normalizes_legacy_pre_v1_keys(self) -> None:
        args, telemetry = extract_telemetry_arguments(
            {
                "customer_id": "cus_123",
                "telemetry": {
                    "intent": "look up customer",
                    "context": "user asked about billing",
                    "frustration_level": "medium",
                },
            }
        )

        self.assertEqual(args, {"customer_id": "cus_123"})
        self.assertEqual(
            telemetry,
            {
                "user_intent": "look up customer",
                "agent_thinking": "user asked about billing",
                "user_frustration": "medium",
            },
        )

    def test_extract_telemetry_ignores_cached_user_turn(self) -> None:
        for cached in (1.9, 0, -1, True, 2.0):
            _, telemetry = extract_telemetry_arguments(
                {"telemetry": {"user_intent": "check account", "user_turn": cached}}
            )
            self.assertEqual(
                telemetry,
                {"user_intent": "check account"},
                f"user_turn={cached!r}",
            )

    def test_append_telemetry_hint_is_idempotent(self) -> None:
        once = append_telemetry_hint("Look up a customer.")
        twice = append_telemetry_hint(once)
        self.assertEqual(once, twice)

    def test_append_telemetry_hint_leaves_older_generation_hints_unchanged(self) -> None:
        # Earlier-V1 (user_intent only) and pre-V1 (`intent`) hints are
        # recognized so a description written by an older SDK build does not
        # accumulate a second, mixed-generation nudge.
        v1_hinted = (
            "Look up a customer.\n\nPass telemetry.user_intent with a one-line "
            "restatement of the user's most recent request."
        )
        self.assertEqual(append_telemetry_hint(v1_hinted), v1_hinted)
        repeated_intent_hinted = (
            "Look up a customer.\n\nPass telemetry.user_intent with a one-line "
            "restatement of the user's most recent request, and "
            "telemetry.agent_thinking with your reasoning for making this specific call."
        )
        self.assertEqual(
            append_telemetry_hint(repeated_intent_hinted), repeated_intent_hinted
        )
        legacy_hinted = (
            "Look up a customer.\n\nPass telemetry.intent with a one-line user "
            "intent for analytics."
        )
        self.assertEqual(append_telemetry_hint(legacy_hinted), legacy_hinted)

    def test_append_telemetry_hint_leaves_pre_v1_hint_alone(self) -> None:
        # A description that reached us through a pre-V1 wrapper keeps its old
        # hint without gaining a second, mixed-generation one.
        legacy = (
            "Look up a customer.\n\n"
            "Pass telemetry.intent with a one-line user intent for analytics."
        )
        self.assertEqual(append_telemetry_hint(legacy), legacy)


if __name__ == "__main__":
    unittest.main()
