from __future__ import annotations

import asyncio
import importlib
import unittest

from armature_mcp_analytics import instrument_fastmcp


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
        self.assertIn("telemetry.intent", registered["kwargs"]["description"])

        result = asyncio.run(lookup_customer(customer_id="cus_123", telemetry={"intent": "find customer"}))
        self.assertEqual(result, {"customer_id": "cus_123"})

        asyncio.run(instrumentation.recorder.flush())
        tool_call = [event for batch in batches for event in batch["events"] if event["kind"] == "tool_call"][0]
        self.assertEqual(tool_call["metadata"]["intent"], "find customer")

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
            return await sync_lookup(customer_id="cus_loop", telemetry={"intent": "call sync in event loop"})

        result = asyncio.run(call_from_loop())

        self.assertEqual(result, {"customer_id": "cus_loop"})
        tool_call = [event for batch in batches for event in batch["events"] if event["kind"] == "tool_call"][0]
        self.assertEqual(tool_call["metadata"]["intent"], "call sync in event loop")

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

        result = asyncio.run(optional_lookup(second="provided", telemetry={"intent": "test binding"}))

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
                telemetry={"intent": "ignore unknown field"},
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

        result = asyncio.run(ping(message="hello", telemetry={"intent": "ping server"}))
        self.assertEqual(result, {"message": "hello"})
        self.assertEqual(batches[0]["events"][0]["metadata"]["tool_name"], "ping")

    def assert_fastmcp_import_path_registers_tool_with_telemetry_schema(self, import_path: str) -> None:
        try:
            module_name, class_name = import_path.rsplit(".", 1)
            module = importlib.import_module(module_name)
            FastMCP = getattr(module, class_name)
        except ImportError:
            self.skipTest(f"{import_path} optional dependency is not installed")

        batches = []
        mcp = FastMCP("analytics-test")
        instrument_fastmcp(
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

        async def run() -> None:
            tools = await mcp.list_tools()
            lookup = next(tool for tool in tools if tool.name == "lookup_customer")
            input_schema = (
                getattr(lookup, "parameters", None)
                or getattr(lookup, "inputSchema", None)
                or getattr(lookup, "input_schema", None)
            )
            self.assertIsNotNone(input_schema)
            self.assertIn("telemetry", input_schema["properties"])
            self.assertIn("telemetry.intent", lookup.description)

            result = await mcp.call_tool(
                "lookup_customer",
                {"customer_id": "cus_real", "telemetry": {"intent": "real FastMCP"}},
            )
            structured = getattr(result[0], "text", None) if isinstance(result, list) else None
            self.assertTrue(structured is not None or result is not None)

        asyncio.run(run())
        tool_call = [event for batch in batches for event in batch["events"] if event["kind"] == "tool_call"][0]
        self.assertEqual(tool_call["metadata"]["intent"], "real FastMCP")

    def test_external_fastmcp_import_path_registers_tool_with_telemetry_schema(self) -> None:
        self.assert_fastmcp_import_path_registers_tool_with_telemetry_schema("fastmcp.FastMCP")

    def test_official_sdk_fastmcp_import_path_registers_tool_with_telemetry_schema(self) -> None:
        self.assert_fastmcp_import_path_registers_tool_with_telemetry_schema("mcp.server.fastmcp.FastMCP")


if __name__ == "__main__":
    unittest.main()
