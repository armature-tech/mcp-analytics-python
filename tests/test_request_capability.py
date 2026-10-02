from __future__ import annotations

import asyncio
import unittest

from armature_mcp_analytics import create_analytics_recorder, instrument_fastmcp


DESCRIPTION = (
    "Records that the user asked for something these tools cannot do, so the "
    "developers of this server can add it. It changes no data and contacts no one. "
    "Call it whenever you cannot do what the user asked with these tools, including "
    "when you send them to an app, a website or a manual step instead. Then answer "
    "them as usual."
)
CURRENT_HINT = '\n\nInclude telemetry.call_purpose with a short description of this action. Include telemetry.user_intent and telemetry.user_frustration only on the first tool call after each new user message.'
REQUEST_CAPABILITY_HINT = CURRENT_HINT + ' Call request_capability before you tell the user something can\'t be done here or has to be done elsewhere.'
TELEMETRY_ONLY_HINT = CURRENT_HINT


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


class RequestCapabilityTests(unittest.TestCase):
    def test_recorder_off_without_sink_or_when_disabled(self) -> None:
        # No delivery path configured -> nothing is injected regardless of the
        # default, and an explicit global disable also suppresses it.
        self.assertFalse(create_analytics_recorder().has_tool("request_capability"))
        disabled = create_analytics_recorder(
            {"armature": {"enabled": False, "request_capability": True}}
        )
        self.assertFalse(disabled.has_tool("request_capability"))

    def test_recorder_on_by_default_once_a_sink_is_configured(self) -> None:
        on = create_analytics_recorder({"armature": {"emit": lambda _b: None}})
        self.assertTrue(on.has_tool("request_capability"))
        # Explicit opt-out disables it (either alias).
        off = create_analytics_recorder(
            {"armature": {"emit": lambda _b: None, "request_capability": False}}
        )
        self.assertFalse(off.has_tool("request_capability"))
        off_camel = create_analytics_recorder(
            {"armature": {"emit": lambda _b: None, "requestCapability": False}}
        )
        self.assertFalse(off_camel.has_tool("request_capability"))

    def test_default_on_yields_to_a_customer_tool(self) -> None:
        recorder = create_analytics_recorder(
            {"armature": {"emit": lambda _batch: None}}
        )
        # On by default (not explicitly opted in): a customer tool of the same
        # name is allowed and takes precedence instead of being reserved.
        recorder.tool({"name": "request_capability"}, lambda _args: None)
        self.assertTrue(recorder.has_tool("request_capability"))

    def test_recorder_definition_dispatch_and_analytics(self) -> None:
        batches = []
        recorder = create_analytics_recorder(
            {
                "armature": {
                    "request_capability": True,
                    "delivery": "await",
                    "actor_id": "capability-actor",
                    "emit": batches.append,
                }
            }
        )
        definition = recorder.tool_definitions()[0]
        self.assertEqual(definition["name"], "request_capability")
        self.assertEqual(definition["description"], DESCRIPTION)
        self.assertEqual(
            definition["annotations"],
            {"title": "Request capability", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        )
        self.assertEqual(set(definition["inputSchema"]["properties"]), {"capability"})
        self.assertEqual(
            definition["inputSchema"]["properties"]["capability"]["minLength"],
            1,
        )
        self.assertEqual(
            definition["inputSchema"]["properties"]["capability"]["description"],
            "One English sentence describing the missing capability needed for the user's task. Translate the summary into English even when the user writes in another language. Describe generic actions and roles. Omit names, contacts, IDs, credentials and all tool argument values.",
        )
        self.assertNotIn("telemetry", definition["inputSchema"]["properties"])

        result = asyncio.run(
            recorder.dispatch(
                "request_capability",
                {"capability": "send a Slack message"},
                {"session_id": "capability-session"},
            )
        )
        self.assertEqual(result, "Capability request acknowledged.")
        tool_call = next(
            event
            for batch in batches
            for event in batch["events"]
            if event["kind"] == "tool_call"
        )
        self.assertEqual(tool_call["metadata"]["tool_name"], "request_capability")
        self.assertIs(tool_call["metadata"]["capability_request"], True)

        with self.assertRaisesRegex(ValueError, "non-empty string"):
            asyncio.run(recorder.dispatch("request_capability", {"capability": "   "}))

    def test_recorder_rejects_name_collision(self) -> None:
        recorder = create_analytics_recorder(
            {"armature": {"requestCapability": True, "emit": lambda _batch: None}}
        )
        with self.assertRaisesRegex(ValueError, "reserved"):
            recorder.tool({"name": "request_capability"}, lambda _args: None)

    def test_injection_is_suppressed_without_a_delivery_path(self) -> None:
        recorder = create_analytics_recorder(
            {"armature": {"request_capability": True, "api_key": ""}}
        )
        self.assertFalse(recorder.has_tool("request_capability"))

    def test_fastmcp_injects_exact_contract(self) -> None:
        batches = []
        mcp = FakeFastMCP()
        instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "request_capability": True,
                    "delivery": "await",
                    "actor_id": "capability-fastmcp",
                    "emit": batches.append,
                }
            },
        )
        registered = mcp.tools["request_capability"]
        self.assertEqual(registered["kwargs"]["description"], DESCRIPTION)
        self.assertEqual(
            registered["kwargs"]["input_schema"]["required"],
            ["capability"],
        )
        result = asyncio.run(registered["func"](capability="upload a file"))
        self.assertEqual(result, "Capability request acknowledged.")
        self.assertEqual(
            [
                event["metadata"]["tool_name"]
                for batch in batches
                for event in batch["events"]
                if event["kind"] == "tool_call"
            ],
            ["request_capability"],
        )
        tool_call = next(
            event
            for batch in batches
            for event in batch["events"]
            if event["kind"] == "tool_call"
        )
        self.assertIs(tool_call["metadata"]["capability_request"], True)

    def test_fastmcp_rejects_existing_and_later_name_collisions(self) -> None:
        existing = FakeFastMCP()
        existing.tools["request_capability"] = {"func": lambda: None, "kwargs": {}}
        config = {
            "armature": {
                "request_capability": True,
                "emit": lambda _batch: None,
            }
        }
        with self.assertRaisesRegex(ValueError, "reserved"):
            instrument_fastmcp(existing, config)

        instrumented = FakeFastMCP()
        instrument_fastmcp(instrumented, config)
        with self.assertRaisesRegex(ValueError, "reserved"):
            instrumented.tool(name="request_capability")(lambda: None)


class RequestCapabilityHintTests(unittest.TestCase):
    """Injected-mode tool descriptions advertise request_capability exactly
    when the SDK enables its request_capability tool."""

    def test_fastmcp_tools_point_to_request_capability_when_enabled(self) -> None:
        mcp = FakeFastMCP()
        instrument_fastmcp(mcp, {"armature": {"emit": lambda _batch: None}})

        @mcp.tool(name="lookup_customer")
        def lookup_customer(customer_id: str) -> dict:
            """Look up a customer."""
            return {"customer_id": customer_id}

        self.assertEqual(
            mcp.tools["lookup_customer"]["kwargs"]["description"],
            "Look up a customer." + REQUEST_CAPABILITY_HINT,
        )
        # The SDK-owned tool itself stays undecorated.
        self.assertEqual(mcp.tools["request_capability"]["kwargs"]["description"], DESCRIPTION)

    def test_fastmcp_tools_keep_current_hint_when_disabled(self) -> None:
        for key in ("request_capability", "requestCapability"):
            with self.subTest(key=key):
                mcp = FakeFastMCP()
                instrument_fastmcp(mcp, {"armature": {"emit": lambda _batch: None, key: False}})

                @mcp.tool(name="lookup_customer")
                def lookup_customer(customer_id: str) -> dict:
                    """Look up a customer."""
                    return {"customer_id": customer_id}

                self.assertNotIn("request_capability", mcp.tools)
                self.assertEqual(
                    mcp.tools["lookup_customer"]["kwargs"]["description"],
                    "Look up a customer." + CURRENT_HINT,
                )

    def test_fastmcp_leaves_an_already_hinted_description_alone(self) -> None:
        mcp = FakeFastMCP()
        instrument_fastmcp(mcp, {"armature": {"emit": lambda _batch: None}})
        hinted = "Look up a customer." + REQUEST_CAPABILITY_HINT

        @mcp.tool(name="lookup_customer", description=hinted)
        def lookup_customer(customer_id: str) -> dict:
            return {"customer_id": customer_id}

        self.assertEqual(mcp.tools["lookup_customer"]["kwargs"]["description"], hinted)

    def test_recorder_tool_definitions_follow_the_same_predicate(self) -> None:
        def definitions(config):
            recorder = create_analytics_recorder(config)
            recorder.tool(
                {"name": "lookup_customer", "description": "Look up a customer."},
                lambda _args, _context: None,
            )
            return {item["name"]: item for item in recorder.tool_definitions()}

        enabled = definitions({"armature": {"emit": lambda _batch: None}})
        self.assertEqual(
            enabled["lookup_customer"]["description"],
            "Look up a customer." + REQUEST_CAPABILITY_HINT,
        )
        self.assertEqual(enabled["request_capability"]["description"], DESCRIPTION)

        disabled = definitions({"armature": {"emit": lambda _batch: None, "request_capability": False}})
        self.assertNotIn("request_capability", disabled)
        self.assertEqual(
            disabled["lookup_customer"]["description"],
            "Look up a customer." + CURRENT_HINT,
        )

    def test_adapters_apply_the_description_length_guard(self) -> None:
        # One byte past the full hint's room: only the telemetry sentence is
        # appended. One byte past that sentence's room: left unchanged. The
        # telemetry schema is injected either way.
        partial = "p" * (1024 - len(REQUEST_CAPABILITY_HINT.encode("utf-8")) + 1)
        unchanged = "u" * (1024 - len(TELEMETRY_ONLY_HINT.encode("utf-8")) + 1)
        config = {"armature": {"emit": lambda _batch: None}}

        mcp = FakeFastMCP()
        instrument_fastmcp(mcp, config)
        with self.assertLogs("armature_mcp_analytics", level="WARNING") as logs:
            for name, description in (("partial_fastmcp", partial), ("unchanged_fastmcp", unchanged)):

                def noop() -> dict:
                    return {}

                mcp.tool(
                    name=name,
                    description=description,
                    input_schema={"type": "object", "properties": {}},
                )(noop)
        self.assertEqual(
            mcp.tools["partial_fastmcp"]["kwargs"]["description"], partial + TELEMETRY_ONLY_HINT
        )
        self.assertEqual(mcp.tools["unchanged_fastmcp"]["kwargs"]["description"], unchanged)
        for name in ("partial_fastmcp", "unchanged_fastmcp"):
            self.assertIn("telemetry", mcp.tools[name]["kwargs"]["input_schema"]["properties"])
        self.assertEqual(len(logs.records), 2)
        self.assertIn('Tool "partial_fastmcp" description is too long for the full', logs.output[0])
        self.assertIn('Tool "unchanged_fastmcp" description is too long to append', logs.output[1])

        recorder = create_analytics_recorder(config)
        for name, description in (("partial_recorder", partial), ("unchanged_recorder", unchanged)):
            recorder.tool({"name": name, "description": description}, lambda _args, _context: None)
        with self.assertLogs("armature_mcp_analytics", level="WARNING") as logs:
            definitions = {item["name"]: item for item in recorder.tool_definitions()}
        self.assertEqual(definitions["partial_recorder"]["description"], partial + TELEMETRY_ONLY_HINT)
        self.assertEqual(definitions["unchanged_recorder"]["description"], unchanged)
        for name in ("partial_recorder", "unchanged_recorder"):
            self.assertIn("telemetry", definitions[name]["inputSchema"]["properties"])
        self.assertEqual(len(logs.records), 2)


if __name__ == "__main__":
    unittest.main()
