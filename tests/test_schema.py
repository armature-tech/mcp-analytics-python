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
        self.assertEqual(telemetry_props["user_turn"]["type"], "integer")
        self.assertEqual(telemetry_props["user_intent"]["type"], "string")
        self.assertEqual(telemetry_props["agent_thinking"]["type"], "string")
        self.assertEqual(telemetry_props["user_frustration"]["type"], "string")

    def test_required_telemetry_mode_is_internal_but_supported(self) -> None:
        decorated = decorate_input_schema_with_telemetry(
            {"type": "object", "properties": {}, "required": []},
            {"telemetry": {"user_intent": "required"}},
        )

        self.assertEqual(decorated["required"], ["telemetry"])
        telemetry = decorated["properties"]["telemetry"]
        # user_intent required, satisfiable via the legacy `intent` spelling so
        # strict validators don't reject cached pre-V1 clients.
        self.assertEqual(
            telemetry["anyOf"],
            [{"required": ["user_intent"]}, {"required": ["intent"]}],
        )
        self.assertNotIn("required", telemetry)

    def test_pre_v1_strict_config_key_still_enables_strict_mode(self) -> None:
        decorated = decorate_input_schema_with_telemetry(
            {"type": "object", "properties": {}, "required": []},
            {"telemetry": {"intent": "required"}},
        )

        self.assertEqual(decorated["required"], ["telemetry"])
        self.assertEqual(
            decorated["properties"]["telemetry"]["anyOf"],
            [{"required": ["user_intent"]}, {"required": ["intent"]}],
        )

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

    def test_extract_telemetry_keeps_only_one_based_integral_user_turn(self) -> None:
        # Fractional, zero, and negative turn counts are dropped rather than
        # coerced, so a bad value never attaches calls to a wrong or
        # nonexistent turn; integral floats (2.0, as some JSON stacks produce)
        # are accepted. Booleans are never turns.
        for bad in (1.9, 0, -1, True):
            _, telemetry = extract_telemetry_arguments(
                {"telemetry": {"user_intent": "check account", "user_turn": bad}}
            )
            self.assertEqual(telemetry, {"user_intent": "check account"}, f"user_turn={bad!r}")

        _, integral = extract_telemetry_arguments(
            {"telemetry": {"user_intent": "check account", "user_turn": 2.0}}
        )
        self.assertEqual(integral, {"user_intent": "check account", "user_turn": 2})

    def test_append_telemetry_hint_is_idempotent(self) -> None:
        once = append_telemetry_hint("Look up a customer.")
        twice = append_telemetry_hint(once)
        self.assertEqual(once, twice)

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
