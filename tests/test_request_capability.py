from __future__ import annotations

import asyncio
import unittest

from armature_mcp_analytics import create_analytics_recorder, instrument_fastmcp


DESCRIPTION = (
    "Request a capability that is not provided by the currently available tools. "
    "Use this when a capability is required to complete the user’s request and no "
    "existing tool can perform it."
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


class RequestCapabilityTests(unittest.TestCase):
    def test_recorder_injection_is_opt_in_and_respects_global_disable(self) -> None:
        self.assertFalse(create_analytics_recorder().has_tool("request_capability"))
        disabled = create_analytics_recorder(
            {"armature": {"enabled": False, "request_capability": True}}
        )
        self.assertFalse(disabled.has_tool("request_capability"))

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
        self.assertEqual(set(definition["inputSchema"]["properties"]), {"capability"})
        self.assertEqual(
            definition["inputSchema"]["properties"]["capability"]["minLength"],
            1,
        )
        self.assertEqual(
            definition["inputSchema"]["properties"]["capability"]["description"],
            "The capability required to complete the user's request. Omit "
            "argument values, PII, and secrets. Use English.",
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


if __name__ == "__main__":
    unittest.main()
