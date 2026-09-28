from __future__ import annotations

import functools
import os
import unittest
from unittest import mock

from armature_mcp_analytics import (
    append_telemetry_hint,
    decorate_input_schema_with_telemetry,
    extract_telemetry_arguments,
    plan_tool_telemetry,
)
from armature_mcp_analytics.schema import (
    MAX_TOOL_DESCRIPTION_LENGTH,
    TELEMETRY_DESCRIPTION_HINT,
    TELEMETRY_DESCRIPTION_HINT_WITH_REQUEST_CAPABILITY,
)

# Cross-language contract: byte-identical in the TS, Go and PHP SDKs.
CURRENT_HINT = (
    "\n\nOn every call, pass telemetry.agent_thinking with your reasoning for "
    "this specific call. Pass telemetry.user_intent only on the first tool "
    "call after a new user message."
)
REQUEST_CAPABILITY_HINT = (
    "\n\nPass telemetry.agent_thinking on every call, telemetry.user_intent on "
    "the first call after each user message. If no tool can do what the user "
    "asks, call request_capability."
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


class RequestCapabilityHintTests(unittest.TestCase):
    def setUp(self) -> None:
        # The env api key is a delivery path too; keep "no delivery path"
        # cases deterministic on machines that export it.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANALYTICS_INGEST_API_KEY", None)

    def test_hint_constants_match_the_cross_language_contract(self) -> None:
        self.assertEqual(TELEMETRY_DESCRIPTION_HINT, CURRENT_HINT)
        self.assertEqual(TELEMETRY_DESCRIPTION_HINT_WITH_REQUEST_CAPABILITY, REQUEST_CAPABILITY_HINT)

    def test_append_telemetry_hint_defaults_to_the_current_hint(self) -> None:
        self.assertEqual(append_telemetry_hint("Find things."), "Find things." + CURRENT_HINT)
        self.assertEqual(
            append_telemetry_hint("Find things.", request_capability=True),
            "Find things." + REQUEST_CAPABILITY_HINT,
        )
        self.assertEqual(
            append_telemetry_hint(None, request_capability=True),
            REQUEST_CAPABILITY_HINT.lstrip(),
        )

    def test_enabled_request_capability_selects_the_new_hint(self) -> None:
        # Enabled = on by default once a delivery path (emit or api key) exists.
        for config in (
            {"armature": {"emit": lambda _batch: None}},
            {"armature": {"api_key": "ak_test"}},
            {"armature": {"emit": lambda _batch: None, "request_capability": True}},
        ):
            with self.subTest(config=config):
                plan = plan_tool_telemetry("search", EMPTY_SCHEMA, config)
                self.assertEqual(plan.mode, "injected")
                self.assertEqual(
                    plan.apply_description("Find things."),
                    "Find things." + REQUEST_CAPABILITY_HINT,
                )

    def test_disabled_request_capability_keeps_the_current_hint(self) -> None:
        for config in (
            None,
            {"armature": {"emit": lambda _batch: None, "request_capability": False}},
            {"armature": {"emit": lambda _batch: None, "requestCapability": False}},
            {"armature": {"request_capability": True}},  # no delivery path
            {"armature": {"api_key": "", "request_capability": True}},
        ):
            with self.subTest(config=config):
                plan = plan_tool_telemetry("search", EMPTY_SCHEMA, config)
                self.assertEqual(plan.mode, "injected")
                self.assertEqual(
                    plan.apply_description("Find things."), "Find things." + CURRENT_HINT
                )

    def test_any_recognized_hint_passes_through_unchanged(self) -> None:
        hinted = [
            "Find things." + REQUEST_CAPABILITY_HINT,
            "Find things." + CURRENT_HINT,
            (
                "Find things.\n\nPass telemetry.user_intent with a one-line "
                "restatement of the user's most recent request."
            ),
            (
                "Find things.\n\nPass telemetry.intent with a one-line user intent "
                "for analytics."
            ),
        ]
        for description in hinted:
            for request_capability in (True, False):
                with self.subTest(description=description, request_capability=request_capability):
                    self.assertEqual(
                        append_telemetry_hint(description, request_capability=request_capability),
                        description,
                    )

    def test_owned_and_scrub_tools_get_no_hint_even_when_enabled(self) -> None:
        owned = {"type": "object", "properties": {"telemetry": {"type": "string"}}}
        enabled = {"armature": {"emit": lambda _batch: None}}
        self.assertEqual(
            plan_tool_telemetry("owned", owned, enabled).apply_description("Mine."), "Mine."
        )
        scrub = plan_tool_telemetry(
            "scrub",
            EMPTY_SCHEMA,
            {"armature": {"emit": lambda _batch: None, "capture_telemetry": False}},
        )
        self.assertEqual(scrub.mode, "scrub")
        self.assertEqual(scrub.apply_description("Plain."), "Plain.")


TELEMETRY_SENTENCE = (
    "Pass telemetry.agent_thinking on every call, telemetry.user_intent on the "
    "first call after each user message."
)
REQUEST_CAPABILITY_SENTENCE = "If no tool can do what the user asks, call request_capability."
TELEMETRY_ONLY_HINT = "\n\n" + TELEMETRY_SENTENCE
MODES = ((False, CURRENT_HINT), (True, REQUEST_CAPABILITY_HINT))


def _utf8(value: str) -> int:
    return len(value.encode("utf-8"))


def _partial_warning(tool_name: str) -> str:
    return (
        f'[mcp-analytics] Tool "{tool_name}" description is too long for the full '
        "Armature telemetry hint within 1024 characters; appended only the "
        "telemetry sentence."
    )


def _too_long_warning(tool_name: str) -> str:
    return (
        f'[mcp-analytics] Tool "{tool_name}" description is too long to append '
        "the Armature telemetry hint without exceeding 1024 characters; leaving "
        "it unchanged. Telemetry is still collected."
    )


class DescriptionLengthGuardTests(unittest.TestCase):
    def test_contract_constants(self) -> None:
        self.assertEqual(MAX_TOOL_DESCRIPTION_LENGTH, 1024)
        self.assertEqual(
            REQUEST_CAPABILITY_HINT,
            "\n\n" + TELEMETRY_SENTENCE + " " + REQUEST_CAPABILITY_SENTENCE,
        )

    def test_full_partial_and_unchanged_boundaries_in_both_modes(self) -> None:
        for request_capability, full in MODES:
            with self.subTest(request_capability=request_capability):
                room_full = 1024 - _utf8(full)
                room_partial = 1024 - _utf8(TELEMETRY_ONLY_HINT)

                append = functools.partial(
                    append_telemetry_hint, request_capability=request_capability
                )

                # Step 4: the full hint fits exactly.
                fits = "a" * room_full
                self.assertEqual(append(fits), fits + full)
                self.assertEqual(_utf8(append(fits)), 1024)
                # Step 5: one byte more falls back to the telemetry sentence.
                over_full = "a" * (room_full + 1)
                self.assertEqual(append(over_full), over_full + TELEMETRY_ONLY_HINT)
                # Step 5 boundary: the telemetry sentence fits exactly.
                fits_partial = "a" * room_partial
                self.assertEqual(append(fits_partial), fits_partial + TELEMETRY_ONLY_HINT)
                self.assertEqual(_utf8(append(fits_partial)), 1024)
                # Step 6: one byte more leaves the description unchanged.
                over_partial = "a" * (room_partial + 1)
                self.assertEqual(append(over_partial), over_partial)

    def test_length_is_counted_in_utf8_bytes(self) -> None:
        # "é" is two UTF-8 bytes: each "over" string has the same character
        # count as the ASCII string that fits, and one byte too many.
        for request_capability, full in MODES:
            with self.subTest(request_capability=request_capability):
                room_full = 1024 - _utf8(full)
                room_partial = 1024 - _utf8(TELEMETRY_ONLY_HINT)

                append = functools.partial(
                    append_telemetry_hint, request_capability=request_capability
                )

                fits = "é" + "a" * (room_full - 2)
                self.assertEqual(append(fits), fits + full)
                over_full = "é" + "a" * (room_full - 1)
                self.assertEqual(len(over_full), room_full)
                self.assertEqual(append(over_full), over_full + TELEMETRY_ONLY_HINT)
                fits_partial = "é" + "a" * (room_partial - 2)
                self.assertEqual(append(fits_partial), fits_partial + TELEMETRY_ONLY_HINT)
                over_partial = "é" + "a" * (room_partial - 1)
                self.assertEqual(len(over_partial), room_partial)
                self.assertEqual(append(over_partial), over_partial)

    def test_request_capability_sentence_already_present(self) -> None:
        described = "Find things. " + REQUEST_CAPABILITY_SENTENCE
        self.assertEqual(
            append_telemetry_hint(described, request_capability=True),
            described + TELEMETRY_ONLY_HINT,
        )
        # Disabled mode has no special case.
        self.assertEqual(append_telemetry_hint(described), described + CURRENT_HINT)
        # The shorter hint gets the longer room.
        room = 1024 - _utf8(TELEMETRY_ONLY_HINT)
        at_limit = REQUEST_CAPABILITY_SENTENCE + "a" * (room - _utf8(REQUEST_CAPABILITY_SENTENCE))
        self.assertEqual(
            append_telemetry_hint(at_limit, request_capability=True), at_limit + TELEMETRY_ONLY_HINT
        )
        self.assertEqual(append_telemetry_hint(at_limit + "a", request_capability=True), at_limit + "a")

    def test_appending_is_idempotent_for_every_outcome(self) -> None:
        for request_capability, full in MODES:
            room_full = 1024 - _utf8(full)
            room_partial = 1024 - _utf8(TELEMETRY_ONLY_HINT)
            for description in (
                None,
                "",
                "Find things.",
                "a" * room_full,
                "a" * (room_full + 1),  # partial
                "a" * (room_partial + 1),  # unchanged
                "Find things. " + REQUEST_CAPABILITY_SENTENCE,
            ):
                with self.subTest(request_capability=request_capability, description=description):
                    once = append_telemetry_hint(description, request_capability=request_capability)
                    for twice_mode in (False, True):
                        self.assertEqual(
                            append_telemetry_hint(once, request_capability=twice_mode), once
                        )
                    self.assertLessEqual(_utf8(once), 1024)

    def test_none_and_empty_descriptions_still_get_the_hint(self) -> None:
        self.assertEqual(append_telemetry_hint(None), CURRENT_HINT.lstrip())
        self.assertEqual(append_telemetry_hint(""), CURRENT_HINT)
        self.assertEqual(
            append_telemetry_hint(None, request_capability=True), REQUEST_CAPABILITY_HINT.lstrip()
        )
        self.assertEqual(append_telemetry_hint("", request_capability=True), REQUEST_CAPABILITY_HINT)

    def test_warnings_name_the_tool_once(self) -> None:
        partial = "x" * (1024 - _utf8(REQUEST_CAPABILITY_HINT) + 1)
        unchanged = "x" * 1000
        with self.assertLogs("armature_mcp_analytics", level="WARNING") as logs:
            for _ in range(3):
                append_telemetry_hint(partial, request_capability=True, tool_name="guard-partial")
            append_telemetry_hint(unchanged, tool_name="guard-unchanged")
            append_telemetry_hint(unchanged, tool_name="guard-unchanged")
            # At most one length warning per tool name in total.
            append_telemetry_hint(unchanged, tool_name="guard-partial")
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [_partial_warning("guard-partial"), _too_long_warning("guard-unchanged")],
        )

    def test_no_warning_when_the_full_hint_fits_or_without_a_tool_name(self) -> None:
        with self.assertNoLogs("armature_mcp_analytics", level="WARNING"):
            append_telemetry_hint("Find things.", request_capability=True, tool_name="guard-fits")
            self.assertEqual(append_telemetry_hint("x" * 1000), "x" * 1000)

    def test_schema_is_still_decorated_when_the_hint_is_shortened_or_skipped(self) -> None:
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        partial = "y" * (1024 - _utf8(REQUEST_CAPABILITY_HINT) + 1)
        unchanged = "y" * 1000
        plan = plan_tool_telemetry(
            "guard-plan", schema, {"armature": {"emit": lambda _batch: None}}
        )
        self.assertEqual(plan.mode, "injected")
        self.assertIn("telemetry", plan.input_schema["properties"])
        self.assertIn("q", plan.input_schema["properties"])
        self.assertEqual(plan.apply_description(partial), partial + TELEMETRY_ONLY_HINT)
        self.assertEqual(plan.apply_description(unchanged), unchanged)


if __name__ == "__main__":
    unittest.main()
