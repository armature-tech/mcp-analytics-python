from __future__ import annotations

import asyncio
import os
import unittest

from armature_mcp_analytics import instrument_fastmcp
from armature_mcp_analytics.schema import (
    AGENT_THINKING_DESCRIPTION,
    TELEMETRY_PROPERTY_DESCRIPTION,
    USER_FRUSTRATION_DESCRIPTION,
    USER_INTENT_DESCRIPTION,
)

TELEMETRY_FIELD_DESCRIPTIONS = {
    "user_intent": USER_INTENT_DESCRIPTION,
    "agent_thinking": AGENT_THINKING_DESCRIPTION,
    "user_frustration": USER_FRUSTRATION_DESCRIPTION,
}


class FakeFastMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            name = kwargs.get("name") or (args[0] if args and isinstance(args[0], str) else func.__name__)
            self.tools[name] = {"func": func, "kwargs": kwargs}
            return func

        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return decorator(args[0])
        return decorator


class FastMCPAdapterTests(unittest.TestCase):
    def test_instruments_decorator_with_parentheses(self) -> None:
        batches = []
        mcp = FakeFastMCP()
        instrumentation = instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "adapter-actor",
                    "emit": batches.append,
                }
            },
        )

        @mcp.tool(name="lookup_customer", input_schema={"type": "object", "properties": {"customer_id": {"type": "string"}}})
        def lookup_customer(customer_id: str) -> dict:
            """Look up a customer."""
            return {"customer_id": customer_id}

        registered = mcp.tools["lookup_customer"]
        schema = registered["kwargs"]["input_schema"]
        self.assertIn("telemetry", schema["properties"])
        self.assertIn("telemetry.user_intent", registered["kwargs"]["description"])

        result = asyncio.run(lookup_customer(customer_id="cus_123", telemetry={"user_intent": "find customer"}))
        self.assertEqual(result, {"customer_id": "cus_123"})

        asyncio.run(instrumentation.recorder.flush())
        tool_call = [event for batch in batches for event in batch["events"] if event["kind"] == "tool_call"][0]
        self.assertEqual(tool_call["metadata"]["user_intent"], "find customer")

    def test_sync_tool_can_run_inside_event_loop(self) -> None:
        batches = []
        mcp = FakeFastMCP()
        instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "loop-actor",
                    "emit": batches.append,
                }
            },
        )

        @mcp.tool(name="sync_lookup")
        def sync_lookup(customer_id: str) -> dict:
            return {"customer_id": customer_id}

        async def call_from_loop() -> dict:
            return await sync_lookup(customer_id="cus_loop", telemetry={"user_intent": "call sync in event loop"})

        result = asyncio.run(call_from_loop())

        self.assertEqual(result, {"customer_id": "cus_loop"})
        tool_call = [event for batch in batches for event in batch["events"] if event["kind"] == "tool_call"][0]
        self.assertEqual(tool_call["metadata"]["user_intent"], "call sync in event loop")

    def test_omitted_optional_parameters_do_not_shift_later_arguments(self) -> None:
        mcp = FakeFastMCP()
        instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "binding-actor",
                    "emit": lambda _batch: None,
                }
            },
        )

        @mcp.tool(name="optional_lookup")
        def optional_lookup(first: str = "default-first", second: str = "default-second") -> dict:
            return {"first": first, "second": second}

        result = asyncio.run(optional_lookup(second="provided", telemetry={"user_intent": "test binding"}))

        self.assertEqual(result, {"first": "default-first", "second": "provided"})

    def test_unknown_arguments_are_not_forwarded_without_var_kwargs(self) -> None:
        mcp = FakeFastMCP()
        instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "unknown-arg-actor",
                    "emit": lambda _batch: None,
                }
            },
        )

        @mcp.tool(name="strict_lookup")
        def strict_lookup(customer_id: str) -> dict:
            return {"customer_id": customer_id}

        result = asyncio.run(
            strict_lookup(
                customer_id="cus_unknown",
                unexpected_field="ignored",
                telemetry={"user_intent": "ignore unknown field"},
            )
        )

        self.assertEqual(result, {"customer_id": "cus_unknown"})

    def test_instrumentation_is_idempotent(self) -> None:
        first_batches = []
        second_batches = []
        mcp = FakeFastMCP()

        first = instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "first-actor",
                    "emit": first_batches.append,
                }
            },
        )
        second = instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "second-actor",
                    "emit": second_batches.append,
                }
            },
        )

        self.assertIs(first, second)

        @mcp.tool(name="idempotent_lookup")
        def idempotent_lookup(customer_id: str) -> dict:
            return {"customer_id": customer_id}

        result = asyncio.run(idempotent_lookup(customer_id="cus_once"))

        self.assertEqual(result, {"customer_id": "cus_once"})
        tool_calls = [event for batch in first_batches for event in batch["events"] if event["kind"] == "tool_call"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(second_batches, [])

    def test_context_auth_and_headers_reach_actor_resolver(self) -> None:
        resolver_input = {}
        mcp = FakeFastMCP()

        def actor_id(input):
            resolver_input.update(input)
            return "context-actor"

        instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": actor_id,
                    "emit": lambda _batch: None,
                }
            },
        )

        @mcp.tool(name="context_lookup")
        def context_lookup(customer_id: str, ctx=None) -> dict:
            return {"customer_id": customer_id, "has_ctx": ctx is not None}

        result = asyncio.run(
            context_lookup(
                customer_id="cus_ctx",
                ctx={
                    "sessionId": "session-ctx",
                    "authInfo": {"principalId": "principal-ctx"},
                    "requestInfo": {"headers": {"x-armature-test-principal": "principal-ctx"}},
                },
            )
        )

        self.assertEqual(result, {"customer_id": "cus_ctx", "has_ctx": True})
        self.assertEqual(resolver_input["authInfo"]["principalId"], "principal-ctx")
        self.assertEqual(resolver_input["headers"]["x-armature-test-principal"], "principal-ctx")

    def test_instruments_bare_decorator(self) -> None:
        batches = []
        mcp = FakeFastMCP()
        instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "bare-actor",
                    "emit": batches.append,
                }
            },
        )

        @mcp.tool
        async def ping(message: str) -> dict:
            return {"message": message}

        result = asyncio.run(ping(message="hello", telemetry={"user_intent": "ping server"}))
        self.assertEqual(result, {"message": "hello"})
        events = [event for batch in batches for event in batch["events"]]
        tool_call = next(event for event in events if event["kind"] == "tool_call")
        self.assertEqual(tool_call["metadata"]["tool_name"], "ping")

    def skip_missing_dependency(self, dependency: str) -> None:
        # CI's version-matrix legs set ARMATURE_REQUIRE_FASTMCP so a broken
        # install fails loudly instead of skipping the whole point of the leg
        # — an in-place fastmcp 2.x→3.x upgrade once left an importable-but-
        # empty namespace package, and every adapter test "passed" by skipping.
        if os.environ.get("ARMATURE_REQUIRE_FASTMCP"):
            self.fail(f"{dependency} must be importable when ARMATURE_REQUIRE_FASTMCP is set")
        self.skipTest(f"{dependency} optional dependency is not installed")

    def assert_advertised_telemetry_schema(self, input_schema: dict, description: str) -> None:
        # The full V1 telemetry schema must reach the *advertised* inputSchema,
        # not just runtime extraction — calling agents only learn the fields
        # exist from tools/list. FastMCP builds this schema with pydantic from
        # the wrapper's type hints, so a regression here (e.g. the annotation
        # collapsing to a bare anyOf[object, null]) drops every description.
        self.assertIsNotNone(input_schema)
        telemetry = input_schema["properties"].get("telemetry")
        self.assertIsNotNone(telemetry, f"telemetry property missing from advertised schema: {input_schema}")
        self.assertEqual(telemetry.get("description"), TELEMETRY_PROPERTY_DESCRIPTION)
        for field, expected_description in TELEMETRY_FIELD_DESCRIPTIONS.items():
            field_schema = telemetry.get("properties", {}).get(field)
            self.assertIsNotNone(field_schema, f"telemetry.{field} missing from advertised schema: {telemetry}")
            self.assertEqual(field_schema.get("description"), expected_description)
        self.assertIn("telemetry.user_intent", description)

    def test_external_fastmcp_advertises_telemetry_schema_on_the_wire(self) -> None:
        # Runs against whichever fastmcp is installed; CI executes this suite
        # under both the 2.x and 3.x lines, which register tools differently
        # (3.x delegates to a provider; 2.x defers through a partial that
        # re-enters the patched .tool attribute).
        try:
            from fastmcp import Client, FastMCP
        except ImportError:
            self.skip_missing_dependency("fastmcp")

        batches = []
        mcp = FastMCP("analytics-test")
        instrumentation = instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "real-fastmcp-actor",
                    "emit": batches.append,
                }
            },
        )

        @mcp.tool
        def lookup_customer(customer_id: str) -> dict:
            """Look up a customer."""
            return {"customer_id": customer_id}

        @mcp.tool(name="named_lookup", description="Named lookup.")
        def named_lookup(customer_id: str) -> dict:
            return {"customer_id": customer_id}

        async def run() -> None:
            async with Client(mcp) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                # Both registration forms must actually reach the server: on
                # fastmcp 2.x the kwargs form silently registered nothing
                # before the re-entry guard existed.
                self.assertEqual(set(tools), {"lookup_customer", "named_lookup"})
                for tool in tools.values():
                    self.assert_advertised_telemetry_schema(tool.inputSchema, tool.description)

                await client.call_tool(
                    "lookup_customer",
                    {"customer_id": "cus_real", "telemetry": {"user_intent": "real FastMCP"}},
                )
                await client.call_tool(
                    "named_lookup",
                    {"customer_id": "cus_named", "telemetry": {"user_intent": "named form"}},
                )

        asyncio.run(run())
        asyncio.run(instrumentation.recorder.flush())
        intents = [
            event["metadata"].get("user_intent")
            for batch in batches
            for event in batch["events"]
            if event["kind"] == "tool_call"
        ]
        self.assertEqual(intents, ["real FastMCP", "named form"])

    def test_official_sdk_fastmcp_advertises_telemetry_schema(self) -> None:
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError:
            self.skip_missing_dependency("mcp")

        batches = []
        mcp = FastMCP("analytics-test")
        instrumentation = instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "official-sdk-actor",
                    "emit": batches.append,
                }
            },
        )

        @mcp.tool()
        def lookup_customer(customer_id: str) -> dict:
            """Look up a customer."""
            return {"customer_id": customer_id}

        async def run() -> None:
            tools = await mcp.list_tools()
            lookup = next(tool for tool in tools if tool.name == "lookup_customer")
            self.assert_advertised_telemetry_schema(lookup.inputSchema, lookup.description)

            await mcp.call_tool(
                "lookup_customer",
                {"customer_id": "cus_real", "telemetry": {"user_intent": "official SDK"}},
            )

        asyncio.run(run())
        asyncio.run(instrumentation.recorder.flush())
        tool_call = [event for batch in batches for event in batch["events"] if event["kind"] == "tool_call"][0]
        self.assertEqual(tool_call["metadata"]["user_intent"], "official SDK")


if __name__ == "__main__":
    unittest.main()
