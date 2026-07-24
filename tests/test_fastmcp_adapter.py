from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import unittest
from pathlib import Path

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
                    "request_capability": False,
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

    def test_official_sdk_output_schema_and_structured_content_unchanged(self) -> None:
        # QA-03 wrapper-safety guarantee: customer-owned result shapes remain
        # unchanged. The fixture defines its tools under `from __future__
        # import annotations`, so every annotation is a string that only
        # resolves in the fixture module's namespace — the official SDK
        # returns a precomputed __signature__ verbatim without evaluating
        # those strings, which used to degrade a dict return's outputSchema
        # to a {"result": ...} wrapper and a BaseModel return's to nothing.
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError:
            self.skip_missing_dependency("mcp")

        fixture_path = Path(__file__).resolve().parent / "fixtures" / "future_annotations_tools.py"
        spec = importlib.util.spec_from_file_location("armature_future_annotations_fixture", fixture_path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)

        def build(instrument: bool) -> FastMCP:
            mcp = FastMCP("schema-parity")
            if instrument:
                instrument_fastmcp(mcp, {"armature": {"delivery": "await", "request_capability": False, "emit": lambda batch: None}})
            mcp.tool()(fixture.get_customer)
            mcp.tool()(fixture.get_customer_model)
            return mcp

        async def snapshot(mcp: FastMCP) -> dict[str, dict[str, str]]:
            snapshots: dict[str, dict[str, str]] = {}
            for tool in await mcp.list_tools():
                result = await mcp.call_tool(tool.name, {"customer_id": "cust_acme"})
                structured = result[1] if isinstance(result, tuple) else None
                snapshots[tool.name] = {
                    "outputSchema": json.dumps(tool.outputSchema, sort_keys=True),
                    "structuredContent": json.dumps(structured, sort_keys=True),
                }
            return snapshots

        baseline = asyncio.run(snapshot(build(False)))
        instrumented_server = build(True)
        instrumented = asyncio.run(snapshot(instrumented_server))

        self.assertEqual(set(baseline), {"get_customer", "get_customer_model"})
        self.assertEqual(instrumented, baseline)
        # The injected telemetry parameter belongs to the input schema only.
        for tool in asyncio.run(instrumented_server.list_tools()):
            self.assertIn("telemetry", tool.inputSchema.get("properties", {}))
            self.assertNotIn("telemetry", (tool.outputSchema or {}).get("properties", {}))

    def test_capture_off_tolerates_stale_schema_telemetry_on_fastmcp(self) -> None:
        # mcp-tester#1391: with capture_telemetry off, clients still holding
        # the previously advertised injected schema keep sending `telemetry`.
        # fastmcp validates calls against a pydantic model built from the
        # wrapper signature, so scrub mode must accept the argument there
        # while the advertised schema stays byte-identical to baseline and
        # the value is stripped and never delivered.
        try:
            from fastmcp import Client, FastMCP
        except ImportError:
            self.skip_missing_dependency("fastmcp")

        batches: list = []

        def build(instrument: bool) -> FastMCP:
            mcp = FastMCP("capture-off")
            if instrument:
                instrument_fastmcp(
                    mcp,
                    {
                        "armature": {
                            "delivery": "await",
                            "capture_telemetry": False,
                            "request_capability": False,
                            "actor_id": "capture-off-actor",
                            "emit": batches.append,
                        }
                    },
                )

            @mcp.tool
            def ping(message: str) -> dict:
                """Echo a message."""
                return {"message": message}

            return mcp

        async def advertised(mcp: FastMCP) -> tuple[str, str | None]:
            async with Client(mcp) as client:
                tool = (await client.list_tools())[0]
                return json.dumps(tool.inputSchema, sort_keys=True), tool.description

        baseline_schema, baseline_description = asyncio.run(advertised(build(False)))
        scrub_server = build(True)
        scrub_schema, scrub_description = asyncio.run(advertised(scrub_server))
        self.assertEqual(scrub_schema, baseline_schema)
        self.assertEqual(scrub_description, baseline_description)

        async def call_with_stale_telemetry() -> None:
            async with Client(scrub_server) as client:
                result = await client.call_tool(
                    "ping",
                    {"message": "hello", "telemetry": {"user_intent": "stale cached intent"}},
                    raise_on_error=False,
                )
                self.assertFalse(
                    result.is_error,
                    f"stale-schema telemetry must not fail the call: {result.content}",
                )
                plain = await client.call_tool("ping", {"message": "hello"}, raise_on_error=False)
                self.assertFalse(plain.is_error)

        asyncio.run(call_with_stale_telemetry())
        tool_calls = [
            event
            for batch in batches
            for event in batch["events"]
            if event["kind"] == "tool_call"
        ]
        self.assertEqual(len(tool_calls), 2)
        for event in tool_calls:
            metadata = event.get("metadata") or {}
            self.assertIsNone(metadata.get("user_intent"))
            self.assertNotIn("stale cached intent", json.dumps(event))

    def test_capture_off_tolerates_stale_schema_telemetry_on_official_sdk(self) -> None:
        # The official SDK's arg model already ignored the extra argument;
        # pin that so the fastmcp-only signature handling never regresses it.
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError:
            self.skip_missing_dependency("mcp")

        batches: list = []

        def build(instrument: bool) -> FastMCP:
            mcp = FastMCP("capture-off-official")
            if instrument:
                instrument_fastmcp(
                    mcp,
                    {
                        "armature": {
                            "delivery": "await",
                            "capture_telemetry": False,
                            "request_capability": False,
                            "actor_id": "capture-off-actor",
                            "emit": batches.append,
                        }
                    },
                )

            @mcp.tool()
            def ping(message: str) -> dict:
                """Echo a message."""
                return {"message": message}

            return mcp

        async def advertised(mcp: FastMCP) -> str:
            tool = (await mcp.list_tools())[0]
            return json.dumps(tool.inputSchema, sort_keys=True)

        baseline_schema = asyncio.run(advertised(build(False)))
        scrub_server = build(True)
        self.assertEqual(asyncio.run(advertised(scrub_server)), baseline_schema)

        result = asyncio.run(
            scrub_server.call_tool(
                "ping",
                {"message": "hello", "telemetry": {"user_intent": "stale cached intent"}},
            )
        )
        self.assertIsNotNone(result)
        tool_calls = [
            event
            for batch in batches
            for event in batch["events"]
            if event["kind"] == "tool_call"
        ]
        self.assertEqual(len(tool_calls), 1)
        self.assertIsNone((tool_calls[0].get("metadata") or {}).get("user_intent"))
        self.assertNotIn("stale cached intent", json.dumps(tool_calls[0]))


if __name__ == "__main__":
    unittest.main()
