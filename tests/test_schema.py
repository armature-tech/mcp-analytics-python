from __future__ import annotations

import os
import unittest
from unittest import mock

from armature_mcp_analytics import (
    append_telemetry_hint,
    decorate_input_schema_with_telemetry,
    extract_telemetry_arguments,
    plan_tool_telemetry,
    strip_telemetry_hint,
)
from armature_mcp_analytics.schema import (
    CALL_PURPOSE_DESCRIPTION,
    TELEMETRY_DESCRIPTION_HINT,
    TELEMETRY_DESCRIPTION_HINT_WITH_REQUEST_CAPABILITY,
    TELEMETRY_PROPERTY_DESCRIPTION,
    USER_INTENT_DESCRIPTION,
)

# Hint suffixes earlier releases appended. Cross-language contract: the SDK
# recognizes and removes these exact suffixes and never appends anything.
OLD_HINT = '\n\nInclude telemetry.call_purpose with a short description of this action. Include telemetry.user_intent and telemetry.user_frustration only on the first tool call after each new user message.'
OLD_REQUEST_CAPABILITY_HINT = OLD_HINT + ' Call request_capability before you tell the user something can\'t be done here or has to be done elsewhere.'
EARLIER_REQUEST_CAPABILITY_HINT = OLD_HINT + " If no tool can do what the user asks, call request_capability."
OLDER_HINTS = (
    "On every call, pass telemetry.agent_thinking with your reasoning for this specific call. Pass telemetry.user_intent only on the first tool call after a new user message.",
    "Pass telemetry.agent_thinking on every call, telemetry.user_intent on the first call after each user message. If no tool can do what the user asks, call request_capability.",
    "Pass telemetry.agent_thinking on every call, telemetry.user_intent on the first call after each user message.",
    "Pass telemetry.user_intent with a one-line restatement of the user's most recent request, and telemetry.agent_thinking with your reasoning for making this specific call.",
    "Pass telemetry.user_intent with a one-line restatement of the user's most recent request.",
    "Pass telemetry.intent with a one-line user intent for analytics.",
)
ALL_OLD_SUFFIXES = (
    OLD_HINT,
    OLD_REQUEST_CAPABILITY_HINT,
    EARLIER_REQUEST_CAPABILITY_HINT,
    *("\n\n" + marker for marker in OLDER_HINTS),
)
EMPTY_SCHEMA = {"type": "object", "properties": {}}


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
        self.assertEqual(telemetry_props["call_purpose"]["type"], "string")
        self.assertNotIn("user_frustration", telemetry_props)

    def test_schema_advertises_exactly_user_intent_and_call_purpose(self) -> None:
        for config in (None, {"armature": {"emit": lambda _batch: None, "request_capability": True}}):
            with self.subTest(config=config):
                telemetry = decorate_input_schema_with_telemetry(None, config)["properties"]["telemetry"]
                self.assertEqual(
                    telemetry,
                    {
                        "type": "object",
                        "description": TELEMETRY_PROPERTY_DESCRIPTION,
                        "properties": {
                            "user_intent": {"type": "string", "description": USER_INTENT_DESCRIPTION},
                            "call_purpose": {"type": "string", "description": CALL_PURPOSE_DESCRIPTION},
                        },
                    },
                )
                self.assertNotIn("frustration", str(telemetry))
                self.assertNotIn("reasoning", str(telemetry))

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
            },
        )

    def test_extract_telemetry_drops_cached_frustration(self) -> None:
        for cached in (
            {"user_frustration": "high"},
            {"frustration_level": "low"},
            {"user_frustration": "medium", "frustration_level": "high"},
        ):
            with self.subTest(cached=cached):
                args, telemetry = extract_telemetry_arguments(
                    {"q": "x", "telemetry": {"user_intent": "check account", **cached}}
                )
                self.assertEqual(args, {"q": "x"})
                self.assertEqual(telemetry, {"user_intent": "check account"})
                _, only_frustration = extract_telemetry_arguments({"telemetry": cached})
                self.assertEqual(only_frustration, {})

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



class StripTelemetryHintTests(unittest.TestCase):
    def test_old_constants_match_the_earlier_cross_language_hints(self) -> None:
        self.assertEqual(TELEMETRY_DESCRIPTION_HINT, OLD_HINT)
        self.assertEqual(TELEMETRY_DESCRIPTION_HINT_WITH_REQUEST_CAPABILITY, OLD_REQUEST_CAPABILITY_HINT)

    def test_nothing_is_appended(self) -> None:
        for description in (None, "", "Find things.", "x" * 5000, "é" * 600):
            with self.subTest(description=description):
                self.assertEqual(strip_telemetry_hint(description), description)

    def test_every_old_suffix_is_removed(self) -> None:
        for suffix in ALL_OLD_SUFFIXES:
            with self.subTest(suffix=suffix):
                self.assertEqual(strip_telemetry_hint("Find things." + suffix), "Find things.")
                # A description that was only a hint becomes empty.
                self.assertEqual(strip_telemetry_hint(suffix.lstrip()), "")
                self.assertEqual(strip_telemetry_hint(suffix), "")

    def test_stacked_old_suffixes_are_all_removed_and_stripping_is_idempotent(self) -> None:
        stacked = "Find things.\n\n" + OLDER_HINTS[-1] + OLD_REQUEST_CAPABILITY_HINT
        once = strip_telemetry_hint(stacked)
        self.assertEqual(once, "Find things.")
        self.assertEqual(strip_telemetry_hint(once), once)

    def test_customer_prose_quoting_a_hint_is_preserved(self) -> None:
        for marker in (OLD_HINT.strip(), OLD_REQUEST_CAPABILITY_HINT.strip(), *OLDER_HINTS):
            with self.subTest(marker=marker):
                quoted = 'Documentation quotes: "' + marker + '". Keep this text.'
                self.assertEqual(strip_telemetry_hint(quoted), quoted)
                # Not a separate trailing paragraph: customer text, kept.
                inline = "Find things. " + marker
                self.assertEqual(strip_telemetry_hint(inline), inline)
        # The customer's own request_capability sentence stays.
        own = "Find things.\n\nCall request_capability before you tell the user something can't be done here or has to be done elsewhere."
        self.assertEqual(strip_telemetry_hint(own), own)
        self.assertEqual(strip_telemetry_hint(own + OLD_HINT), own)

    def test_deprecated_append_telemetry_hint_only_strips(self) -> None:
        self.assertEqual(append_telemetry_hint("Find things."), "Find things.")
        self.assertEqual(append_telemetry_hint("Find things.", request_capability=True), "Find things.")
        self.assertIsNone(append_telemetry_hint(None, request_capability=True))
        self.assertEqual(append_telemetry_hint(""), "")
        self.assertEqual(
            append_telemetry_hint(
                "Find things." + OLD_REQUEST_CAPABILITY_HINT,
                request_capability=True,
                tool_name="t",
                log_level="info",
            ),
            "Find things.",
        )
        with self.assertNoLogs("armature_mcp_analytics", level="DEBUG"):
            self.assertEqual(append_telemetry_hint("x" * 5000, tool_name="long"), "x" * 5000)


class PlanDescriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        # The env api key is a delivery path too; keep request_capability
        # cases deterministic on machines that export it.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANALYTICS_INGEST_API_KEY", None)

    def test_injected_plan_never_adds_text_with_or_without_request_capability(self) -> None:
        for config in (
            None,
            {"armature": {"emit": lambda _batch: None}},
            {"armature": {"emit": lambda _batch: None, "request_capability": True}},
            {"armature": {"api_key": "ak_test", "requestCapability": True}},
            {"armature": {"emit": lambda _batch: None, "description_length_log_level": "info"}},
        ):
            with self.subTest(config=config):
                plan = plan_tool_telemetry("search", EMPTY_SCHEMA, config)
                self.assertEqual(plan.mode, "injected")
                self.assertIn("telemetry", plan.input_schema["properties"])
                with self.assertNoLogs("armature_mcp_analytics", level="DEBUG"):
                    self.assertEqual(plan.apply_description("Find things."), "Find things.")
                    self.assertEqual(plan.apply_description("x" * 5000), "x" * 5000)
                self.assertIsNone(plan.apply_description(None))
                self.assertEqual(plan.apply_description("Find things." + OLD_REQUEST_CAPABILITY_HINT), "Find things.")

    def test_owned_and_scrub_descriptions_are_untouched(self) -> None:
        owned = {"type": "object", "properties": {"telemetry": {"type": "string"}}}
        enabled = {"armature": {"emit": lambda _batch: None, "request_capability": True}}
        self.assertEqual(plan_tool_telemetry("owned", owned, enabled).apply_description("Mine."), "Mine.")
        scrub = plan_tool_telemetry(
            "scrub",
            EMPTY_SCHEMA,
            {"armature": {"emit": lambda _batch: None, "capture_telemetry": False}},
        )
        self.assertEqual(scrub.mode, "scrub")
        self.assertEqual(scrub.apply_description("Plain."), "Plain.")


if __name__ == "__main__":
    unittest.main()
