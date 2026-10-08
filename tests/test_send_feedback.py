from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

from armature_mcp_analytics import create_analytics_recorder, instrument_fastmcp
from armature_mcp_analytics import capability


DESCRIPTION = (
    "Call this before you tell the user that these tools can't do what they asked. "
    "It records the request so the developers of this server can add it. It changes "
    "no data and contacts no one. Then answer the user as usual."
)
ARGUMENT_DESCRIPTION = "One English sentence describing the missing capability needed for the user's task. Translate the summary into English even when the user writes in another language. Describe generic actions and roles. Omit names, contacts, IDs, credentials and all tool argument values."
ANNOTATIONS = {
    "title": "Send feedback",
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
# Hint suffixes earlier SDK releases appended to every tool description. The
# SDK no longer appends anything; these only exercise suffix removal.
OLD_HINT = '\n\nInclude telemetry.call_purpose with a short description of this action. Include telemetry.user_intent and telemetry.user_frustration only on the first tool call after each new user message.'
OLD_REQUEST_CAPABILITY_HINT = OLD_HINT + ' Call request_capability before you tell the user something can\'t be done here or has to be done elsewhere.'


def _emit(_batch) -> None:
    return None


DEFAULT = {"armature": {"emit": _emit}}
EXPLICIT = {"armature": {"emit": _emit, "send_feedback": True}}
DISABLED_CONFIGS = (
    {"armature": {"emit": _emit, "send_feedback": False}},
    {"armature": {"emit": _emit, "sendFeedback": False}},
    # The deprecated alias still disables it.
    {"armature": {"emit": _emit, "request_capability": False}},
    {"armature": {"emit": _emit, "requestCapability": False}},
    # The new key wins over the old one.
    {"armature": {"emit": _emit, "send_feedback": False, "request_capability": True}},
)


class FakeFastMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(func):
            name = kwargs.get("name") or (args[0] if args and isinstance(args[0], str) else func.__name__)
            self.tools[name] = {"func": func, "kwargs": kwargs}
            return func

        if args and callable(args[0]):
            return register(args[0])
        return register


class PositionalOnlyFastMCP:
    """A registrar that only takes a function: no name/description keywords."""

    def __init__(self) -> None:
        self.tools = {}

    def tool(self, func=None):
        def register(inner):
            self.tools[inner.__name__] = {"func": inner, "kwargs": {}}
            return inner

        return register(func) if func is not None else register


class _NoEnvApiKey(unittest.TestCase):
    def setUp(self) -> None:
        # The env api key is a delivery path too; keep "no delivery path"
        # cases deterministic on machines that export it.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANALYTICS_INGEST_API_KEY", None)


class SendFeedbackConfigTests(_NoEnvApiKey):
    def test_on_by_default_with_a_delivery_path(self) -> None:
        for config in (
            DEFAULT,
            {"armature": {"api_key": "ak_test"}},
            EXPLICIT,
            {"armature": {"emit": _emit, "sendFeedback": True}},
            {"armature": {"emit": _emit, "request_capability": True}},
            # The new key wins over the old one.
            {"armature": {"emit": _emit, "send_feedback": True, "request_capability": False}},
        ):
            with self.subTest(config=config):
                recorder = create_analytics_recorder(config)
                self.assertTrue(recorder.has_tool("send_feedback"))
                self.assertFalse(recorder.has_tool("request_capability"))
                self.assertEqual([item["name"] for item in recorder.tool_definitions()], ["send_feedback"])

    def test_disabled_by_the_new_key_or_the_old_alias(self) -> None:
        for config in DISABLED_CONFIGS:
            with self.subTest(config=config):
                recorder = create_analytics_recorder(config)
                self.assertFalse(recorder.has_tool("send_feedback"))
                self.assertEqual(recorder.tool_definitions(), [])

                mcp = FakeFastMCP()
                instrument_fastmcp(mcp, config)
                self.assertEqual(mcp.tools, {})

    def test_off_without_a_delivery_path_or_when_disabled(self) -> None:
        for config in (
            None,
            {"armature": {"send_feedback": True}},
            {"armature": {"send_feedback": True, "api_key": ""}},
            {"armature": {"enabled": False, "send_feedback": True, "emit": _emit}},
        ):
            with self.subTest(config=config):
                self.assertFalse(create_analytics_recorder(config).has_tool("send_feedback"))

    def test_explicit_and_default_resolution(self) -> None:
        self.assertTrue(capability.send_feedback_enabled(DEFAULT))
        self.assertFalse(capability.send_feedback_explicit(DEFAULT))
        self.assertTrue(capability.send_feedback_explicit(EXPLICIT))
        self.assertTrue(capability.send_feedback_explicit({"armature": {"request_capability": True}}))
        self.assertFalse(
            capability.send_feedback_explicit(
                {"armature": {"send_feedback": False, "request_capability": True}}
            )
        )
        # Truthy non-booleans leave it on by default, never explicit.
        self.assertTrue(capability.send_feedback_enabled({"armature": {"emit": _emit, "send_feedback": "yes"}}))
        self.assertFalse(capability.send_feedback_explicit({"armature": {"send_feedback": "yes"}}))

    def test_public_names_and_deprecated_aliases(self) -> None:
        self.assertEqual(capability.SEND_FEEDBACK_TOOL_NAME, "send_feedback")
        self.assertEqual(capability.SEND_FEEDBACK_DESCRIPTION, DESCRIPTION)
        self.assertEqual(capability.SEND_FEEDBACK_ARGUMENT_DESCRIPTION, ARGUMENT_DESCRIPTION)
        self.assertEqual(capability.SEND_FEEDBACK_ANNOTATIONS, ANNOTATIONS)
        self.assertEqual(capability.REQUEST_CAPABILITY_TOOL_NAME, "send_feedback")
        self.assertEqual(capability.REQUEST_CAPABILITY_DESCRIPTION, DESCRIPTION)
        self.assertEqual(capability.REQUEST_CAPABILITY_ANNOTATIONS, ANNOTATIONS)
        self.assertEqual(capability.REQUEST_CAPABILITY_ACKNOWLEDGMENT, capability.SEND_FEEDBACK_ACKNOWLEDGMENT)
        self.assertEqual(capability.request_capability_registration(), capability.send_feedback_registration())
        self.assertIs(capability.request_capability_enabled, capability.send_feedback_enabled)


class SendFeedbackRecorderTests(_NoEnvApiKey):
    def test_definition_dispatch_and_analytics(self) -> None:
        batches = []
        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "feedback-actor",
                    "emit": batches.append,
                }
            }
        )
        definition = recorder.tool_definitions()[0]
        self.assertEqual(definition["name"], "send_feedback")
        self.assertEqual(definition["description"], DESCRIPTION)
        self.assertEqual(definition["annotations"], ANNOTATIONS)
        self.assertEqual(set(definition["inputSchema"]["properties"]), {"capability"})
        capability_schema = definition["inputSchema"]["properties"]["capability"]
        self.assertEqual(capability_schema["minLength"], 1)
        self.assertEqual(capability_schema["maxLength"], 1000)
        self.assertEqual(capability_schema["description"], ARGUMENT_DESCRIPTION)
        self.assertEqual(definition["inputSchema"]["required"], ["capability"])
        self.assertNotIn("telemetry", definition["inputSchema"]["properties"])

        result = asyncio.run(
            recorder.dispatch(
                "send_feedback",
                {"capability": "send a Slack message"},
                {"session_id": "feedback-session"},
            )
        )
        self.assertEqual(result, "Capability request acknowledged.")
        tool_call = next(
            event
            for batch in batches
            for event in batch["events"]
            if event["kind"] == "tool_call"
        )
        self.assertEqual(tool_call["metadata"]["tool_name"], "send_feedback")
        self.assertIs(tool_call["metadata"]["capability_request"], True)

        with self.assertRaisesRegex(ValueError, "non-empty string"):
            asyncio.run(recorder.dispatch("send_feedback", {"capability": "   "}))
        with self.assertRaises(KeyError):
            asyncio.run(recorder.dispatch("request_capability", {"capability": "x"}))

    def test_default_on_yields_to_a_customer_tool(self) -> None:
        batches = []
        recorder = create_analytics_recorder(
            {"armature": {"delivery": "await", "actor_id": "a", "emit": batches.append}}
        )
        recorder.tool({"name": "send_feedback"}, lambda _args, _context: "mine")
        self.assertEqual(asyncio.run(recorder.dispatch("send_feedback", {})), "mine")
        tool_call = next(e for b in batches for e in b["events"] if e["kind"] == "tool_call")
        self.assertNotIn("capability_request", tool_call["metadata"])

    def test_explicit_true_rejects_a_name_collision(self) -> None:
        for config in (
            EXPLICIT,
            {"armature": {"emit": _emit, "sendFeedback": True}},
            {"armature": {"emit": _emit, "requestCapability": True}},
        ):
            with self.subTest(config=config):
                recorder = create_analytics_recorder(config)
                with self.assertRaisesRegex(ValueError, "'send_feedback' is reserved.*send_feedback"):
                    recorder.tool({"name": "send_feedback"}, lambda _args, _context: None)

    def test_the_old_name_is_an_ordinary_customer_tool(self) -> None:
        recorder = create_analytics_recorder(EXPLICIT)
        recorder.tool({"name": "request_capability"}, lambda _args, _context: "mine")
        self.assertEqual(asyncio.run(recorder.dispatch("request_capability", {})), "mine")


class SendFeedbackFastMCPTests(_NoEnvApiKey):
    def test_injects_exact_contract_and_records_the_call(self) -> None:
        batches = []
        mcp = FakeFastMCP()
        instrument_fastmcp(
            mcp,
            {"armature": {"delivery": "await", "actor_id": "feedback-fastmcp", "emit": batches.append}},
        )
        self.assertEqual(list(mcp.tools), ["send_feedback"])
        registered = mcp.tools["send_feedback"]
        self.assertEqual(registered["kwargs"]["name"], "send_feedback")
        self.assertEqual(registered["kwargs"]["description"], DESCRIPTION)
        self.assertEqual(registered["kwargs"]["input_schema"]["required"], ["capability"])
        annotations = registered["kwargs"]["annotations"]
        if not isinstance(annotations, dict):
            annotations = annotations.model_dump(by_alias=True, exclude_none=True)
        self.assertEqual(annotations, ANNOTATIONS)
        result = asyncio.run(registered["func"](capability="upload a file"))
        self.assertEqual(result, "Capability request acknowledged.")
        tool_calls = [
            event for batch in batches for event in batch["events"] if event["kind"] == "tool_call"
        ]
        self.assertEqual([event["metadata"]["tool_name"] for event in tool_calls], ["send_feedback"])
        self.assertIs(tool_calls[0]["metadata"]["capability_request"], True)

    def test_default_on_yields_to_an_existing_or_later_customer_tool(self) -> None:
        existing = FakeFastMCP()
        existing.tools["send_feedback"] = {"func": lambda: "mine", "kwargs": {}}
        instrument_fastmcp(existing, DEFAULT)
        self.assertEqual(existing.tools["send_feedback"]["kwargs"], {})

        later = FakeFastMCP()
        instrument_fastmcp(later, DEFAULT)

        @later.tool(name="send_feedback")
        def mine() -> str:
            return "mine"

        self.assertEqual(asyncio.run(later.tools["send_feedback"]["func"]()), "mine")

    def test_explicit_true_rejects_existing_and_later_collisions(self) -> None:
        existing = FakeFastMCP()
        existing.tools["send_feedback"] = {"func": lambda: None, "kwargs": {}}
        with self.assertRaisesRegex(ValueError, "'send_feedback' is reserved.*send_feedback"):
            instrument_fastmcp(existing, EXPLICIT)

        instrumented = FakeFastMCP()
        instrument_fastmcp(instrumented, EXPLICIT)
        with self.assertRaisesRegex(ValueError, "'send_feedback' is reserved"):
            instrumented.tool(name="send_feedback")(lambda: None)

    def test_unsupported_server_shape_is_skipped_by_default_and_an_error_when_explicit(self) -> None:
        quiet = PositionalOnlyFastMCP()
        instrument_fastmcp(quiet, DEFAULT)
        self.assertNotIn("send_feedback", quiet.tools)

        with self.assertRaisesRegex(ValueError, "cannot register the send_feedback tool.*send_feedback=False"):
            instrument_fastmcp(PositionalOnlyFastMCP(), EXPLICIT)


class ToolDescriptionTests(_NoEnvApiKey):
    """The SDK never adds text to a tool description, and no other tool
    mentions send_feedback (or request_capability), on or off."""

    CONFIGS = (DEFAULT, EXPLICIT, DISABLED_CONFIGS[0])

    def test_fastmcp_descriptions_are_never_modified(self) -> None:
        long = "Look up a customer. " + "x" * 2000
        for config in self.CONFIGS:
            with self.subTest(config=config):
                mcp = FakeFastMCP()
                instrument_fastmcp(mcp, config)
                with self.assertNoLogs("armature_mcp_analytics", level="DEBUG"):

                    @mcp.tool(name="lookup_customer")
                    def lookup_customer(customer_id: str) -> dict:
                        """Look up a customer."""
                        return {"customer_id": customer_id}

                    @mcp.tool(name="explicit", description="Find things.")
                    def explicit() -> dict:
                        return {}

                    @mcp.tool(name="long", description=long)
                    def long_tool() -> dict:
                        return {}

                    @mcp.tool(name="bare")
                    def bare() -> dict:
                        return {}

                # The telemetry schema is still injected.
                self.assertIn("telemetry", mcp.tools["explicit"]["kwargs"]["input_schema"]["properties"])
                self.assertNotIn("description", mcp.tools["lookup_customer"]["kwargs"])
                self.assertEqual(mcp.tools["lookup_customer"]["func"].__doc__, "Look up a customer.")
                self.assertEqual(mcp.tools["explicit"]["kwargs"]["description"], "Find things.")
                self.assertEqual(mcp.tools["long"]["kwargs"]["description"], long)
                # A tool without a description keeps none.
                self.assertNotIn("description", mcp.tools["bare"]["kwargs"])
                self.assertIsNone(mcp.tools["bare"]["func"].__doc__)
                for name, registered in mcp.tools.items():
                    text = str(registered["kwargs"].get("description") or "")
                    self.assertNotIn("Include telemetry", text)
                    if name != "send_feedback":
                        self.assertNotIn("send_feedback", text)
                        self.assertNotIn("request_capability", text)
                self.assertEqual("send_feedback" in mcp.tools, config is not DISABLED_CONFIGS[0])

    def test_recorder_definitions_are_never_modified(self) -> None:
        long = "Look up a customer. " + "x" * 2000
        for config in self.CONFIGS:
            with self.subTest(config=config):
                recorder = create_analytics_recorder(config)
                recorder.tool({"name": "lookup", "description": "Look up a customer."}, lambda _a, _c: None)
                recorder.tool({"name": "long", "description": long}, lambda _a, _c: None)
                recorder.tool({"name": "empty", "description": ""}, lambda _a, _c: None)
                recorder.tool({"name": "bare"}, lambda _a, _c: None)
                with self.assertNoLogs("armature_mcp_analytics", level="DEBUG"):
                    definitions = {item["name"]: item for item in recorder.tool_definitions()}
                self.assertEqual(definitions["lookup"]["description"], "Look up a customer.")
                self.assertEqual(definitions["long"]["description"], long)
                self.assertEqual(definitions["empty"]["description"], "")
                self.assertNotIn("description", definitions["bare"])
                for name in ("lookup", "long", "empty", "bare"):
                    self.assertIn("telemetry", definitions[name]["inputSchema"]["properties"])
                self.assertEqual("send_feedback" in definitions, config is not DISABLED_CONFIGS[0])
                if "send_feedback" in definitions:
                    self.assertEqual(definitions["send_feedback"]["description"], DESCRIPTION)
                    self.assertNotIn("telemetry", definitions["send_feedback"]["inputSchema"]["properties"])
                for name, definition in definitions.items():
                    if name != "send_feedback":
                        self.assertNotIn("send_feedback", definition.get("description") or "")
                        self.assertNotIn("request_capability", definition.get("description") or "")

    def test_old_sdk_hint_suffixes_are_removed_on_every_path(self) -> None:
        for config in self.CONFIGS:
            for suffix in (OLD_HINT, OLD_REQUEST_CAPABILITY_HINT):
                with self.subTest(config=config, suffix=suffix):
                    mcp = FakeFastMCP()
                    instrument_fastmcp(mcp, config)

                    @mcp.tool(name="kwarg", description="Look up a customer." + suffix)
                    def kwarg() -> dict:
                        return {}

                    def docstring() -> dict:
                        return {}

                    docstring.__doc__ = "Look up a customer." + suffix
                    mcp.tool(name="docstring")(docstring)

                    for name in ("kwarg", "docstring"):
                        self.assertEqual(mcp.tools[name]["kwargs"]["description"], "Look up a customer.")
                        self.assertEqual(mcp.tools[name]["func"].__doc__, "Look up a customer.")

                    recorder = create_analytics_recorder(config)
                    recorder.tool(
                        {"name": "hinted", "description": "Look up a customer." + suffix},
                        lambda _a, _c: None,
                    )
                    recorder.tool({"name": "only_hint", "description": suffix.lstrip()}, lambda _a, _c: None)
                    definitions = {item["name"]: item for item in recorder.tool_definitions()}
                    self.assertEqual(definitions["hinted"]["description"], "Look up a customer.")
                    self.assertEqual(definitions["only_hint"]["description"], "")


if __name__ == "__main__":
    unittest.main()
