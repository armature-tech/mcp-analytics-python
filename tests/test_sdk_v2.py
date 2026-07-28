"""MCP SDK v2 (spec 2026-07-28) support.

Layout mirrors the ecosystem split:

- ladder/envelope unit tests run in every environment (no MCP import);
- ``OfficialSdkV2Tests`` need ``mcp>=2`` (``mcp.server.mcpserver.MCPServer``);
- ``FastMCP4Tests`` need ``fastmcp>=4``;
- guard-warning tests force the version probe so they run everywhere.

CI's v2 legs set ``ARMATURE_REQUIRE_MCP2`` / ``ARMATURE_REQUIRE_FASTMCP4`` so
a broken install fails loudly instead of skipping the point of the leg.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings

from armature_mcp_analytics import instrument_fastmcp
from armature_mcp_analytics import sdk_v2
from armature_mcp_analytics.events import (
    MAX_REQUEST_META_BYTES,
    REQUEST_META_TRUNCATION_MARKER,
    _request_meta_metadata,
)
from armature_mcp_analytics.sdk_v2 import (
    ARMATURE_CTX_KWARG,
    RequestContextCapture,
    capture_from_context_object,
    client_info_from_meta,
    parse_baggage,
    resolve_session_key,
)

try:
    from mcp.server.mcpserver import MCPServer

    HAVE_MCP2_SERVER = True
except Exception:  # pragma: no cover - absence is a valid (v1) environment
    HAVE_MCP2_SERVER = False

try:
    import fastmcp as _fastmcp

    HAVE_FASTMCP4 = int(str(getattr(_fastmcp, "__version__", "0")).split(".")[0]) >= 4
except Exception:  # pragma: no cover
    HAVE_FASTMCP4 = False

if os.environ.get("ARMATURE_REQUIRE_MCP2") and not HAVE_MCP2_SERVER:
    raise AssertionError("ARMATURE_REQUIRE_MCP2 is set but mcp.server.mcpserver is not importable")
if os.environ.get("ARMATURE_REQUIRE_FASTMCP4") and not HAVE_FASTMCP4:
    raise AssertionError("ARMATURE_REQUIRE_FASTMCP4 is set but fastmcp>=4 is not importable")

PROTOCOL_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
CAPS_KEY = "io.modelcontextprotocol/clientCapabilities"


def _modern_meta(
    *,
    client: dict | None = None,
    baggage: str | None = None,
    extra: dict | None = None,
) -> dict:
    meta: dict = {PROTOCOL_KEY: "2026-07-28", CAPS_KEY: {}}
    if client is not None:
        meta[CLIENT_INFO_KEY] = client
    if baggage is not None:
        meta["baggage"] = baggage
    if extra:
        meta.update(extra)
    return meta


class _FakeRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _FakeRequestContext:
    def __init__(self, meta: dict | None, request: _FakeRequest | None, protocol_version: str = "2026-07-28") -> None:
        self.meta = meta
        self.request = request
        self.protocol_version = protocol_version


class _FakeInjectedContext:
    """Duck-typed stand-in for mcp.server.mcpserver.Context."""

    def __init__(self, request_context: _FakeRequestContext | None) -> None:
        self._request_context = request_context

    @property
    def request_context(self) -> _FakeRequestContext:
        if self._request_context is None:
            raise ValueError("Context is not available outside of a request")
        return self._request_context


class _FakeFastMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            name = kwargs.get("name") or (args[0] if args and isinstance(args[0], str) else func.__name__)
            self.tools[name] = {"func": func, "kwargs": kwargs}
            return func

        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return decorator(args[0])
        return decorator


class BaggageAndLadderTests(unittest.TestCase):
    def test_parse_baggage_url_decodes_and_drops_properties(self) -> None:
        parsed = parse_baggage("gen_ai.conversation.id=conv%20one;prop=x, other=plain ,broken")
        self.assertEqual(parsed["gen_ai.conversation.id"], "conv one")
        self.assertEqual(parsed["other"], "plain")
        self.assertNotIn("broken", parsed)

    def test_ladder_baggage_beats_seed_and_legacy_headers(self) -> None:
        session = resolve_session_key(
            {"baggage": "gen_ai.conversation.id=conv-1"},
            {"x-armature-session-seed": "seed-2", "mcp-session-id": "legacy-3"},
        )
        self.assertEqual(session, "conv-1")

    def test_ladder_seed_beats_legacy_header(self) -> None:
        session = resolve_session_key(
            {},
            {"x-armature-session-seed": "seed-2", "mcp-session-id": "legacy-3"},
        )
        self.assertEqual(session, "seed-2")

    def test_ladder_legacy_header_last(self) -> None:
        self.assertEqual(resolve_session_key(None, {"mcp-session-id": "legacy-3"}), "legacy-3")

    def test_ladder_no_signals_is_none(self) -> None:
        self.assertIsNone(resolve_session_key({}, {"user-agent": "qa"}))
        self.assertIsNone(resolve_session_key(None, None))

    def test_client_info_from_meta(self) -> None:
        info = client_info_from_meta(_modern_meta(client={"name": "qa", "version": "1.0"}))
        self.assertEqual(info["name"], "qa")
        self.assertEqual(info["version"], "1.0")
        self.assertEqual(info["protocolVersion"], "2026-07-28")
        self.assertEqual(info["capabilities"], {})

    def test_client_info_absent_yields_versions_only(self) -> None:
        # clientInfo is OPTIONAL on the wire; the required pair is
        # protocolVersion + clientCapabilities. Absent name must not crash
        # and must not fabricate an identity.
        info = client_info_from_meta(_modern_meta())
        self.assertNotIn("name", info)
        self.assertEqual(info["protocolVersion"], "2026-07-28")

    def test_capture_from_context_object_rejects_non_contexts(self) -> None:
        self.assertIsNone(capture_from_context_object(None))
        self.assertIsNone(capture_from_context_object({"sessionId": "x"}))
        self.assertIsNone(capture_from_context_object("string"))

    def test_capture_outside_request_is_stdio_shaped(self) -> None:
        capture = capture_from_context_object(_FakeInjectedContext(None))
        self.assertIsInstance(capture, RequestContextCapture)
        self.assertFalse(capture.has_http_request)
        self.assertIsNone(capture.meta)


class RequestMetaCapTests(unittest.TestCase):
    def test_small_meta_is_verbatim(self) -> None:
        meta = _modern_meta(extra={"traceparent": "00-abc-def-01"})
        self.assertEqual(_request_meta_metadata(meta), {"request_meta": meta})

    def test_oversized_meta_is_truncated_with_marker(self) -> None:
        meta = _modern_meta(extra={"blob": "x" * (MAX_REQUEST_META_BYTES + 100)})
        result = _request_meta_metadata(meta)
        self.assertTrue(result["request_meta_truncated"])
        self.assertIsInstance(result["request_meta"], str)
        self.assertTrue(result["request_meta"].endswith(REQUEST_META_TRUNCATION_MARKER))
        prefix = result["request_meta"][: -len(REQUEST_META_TRUNCATION_MARKER)]
        self.assertLessEqual(len(prefix.encode("utf-8")), MAX_REQUEST_META_BYTES)

    def test_empty_or_non_mapping_meta_is_dropped(self) -> None:
        self.assertEqual(_request_meta_metadata(None), {})
        self.assertEqual(_request_meta_metadata({}), {})
        self.assertEqual(_request_meta_metadata("raw"), {})


class InjectedContextPipelineTests(unittest.TestCase):
    """Wrapped tools fed a fake injected ctx carrying _meta envelope keys.

    Environment-independent: the capture path duck-types the context object,
    so these run under both the v1 and v2 pinned environments.
    """

    def _run_tool(self, ctx, args: dict | None = None) -> list:
        batches: list = []
        mcp = _FakeFastMCP()
        instrumentation = instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "request_capability": False,
                    "actor_id": "v2-fake-actor",
                    "emit": batches.append,
                }
            },
        )

        @mcp.tool(name="lookup")
        def lookup(customer_id: str) -> dict:
            return {"customer_id": customer_id}

        call_args = {"customer_id": "c1", **(args or {})}
        result = asyncio.run(lookup(**call_args, ctx=ctx))
        self.assertEqual(result, {"customer_id": "c1"})
        asyncio.run(instrumentation.recorder.flush())
        return [event for batch in batches for event in batch["events"]]

    def test_baggage_conversation_id_wins_and_emits_session_init(self) -> None:
        ctx = _FakeInjectedContext(
            _FakeRequestContext(
                _modern_meta(
                    client={"name": "qa-modern", "version": "3.2.1"},
                    baggage="gen_ai.conversation.id=conv-777",
                ),
                _FakeRequest({"x-armature-session-seed": "seed-1", "mcp-session-id": "legacy-1"}),
            )
        )
        events = self._run_tool(ctx, {"telemetry": {"user_intent": "modern"}})
        kinds = [event["kind"] for event in events]
        self.assertIn("session_init", kinds)
        self.assertIn("tool_call", kinds)
        for event in events:
            self.assertEqual(event["session_id_hint"], "conv-777")
        session_init = next(event for event in events if event["kind"] == "session_init")
        self.assertEqual(session_init["metadata"]["client_name"], "qa-modern")
        self.assertEqual(session_init["metadata"]["client_version"], "3.2.1")
        self.assertEqual(session_init["metadata"]["protocol_version"], "2026-07-28")
        tool_call = next(event for event in events if event["kind"] == "tool_call")
        self.assertEqual(tool_call["metadata"]["user_intent"], "modern")
        self.assertEqual(
            tool_call["metadata"]["request_meta"]["baggage"],
            "gen_ai.conversation.id=conv-777",
        )

    def test_seed_header_when_no_baggage(self) -> None:
        ctx = _FakeInjectedContext(
            _FakeRequestContext(
                _modern_meta(),
                _FakeRequest({"x-armature-session-seed": "seed-42", "mcp-session-id": "legacy-1"}),
            )
        )
        events = self._run_tool(ctx)
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event["session_id_hint"], "seed-42")

    def test_legacy_session_header_still_honored(self) -> None:
        ctx = _FakeInjectedContext(
            _FakeRequestContext(_modern_meta(), _FakeRequest({"mcp-session-id": "legacy-9"}))
        )
        events = self._run_tool(ctx)
        for event in events:
            self.assertEqual(event["session_id_hint"], "legacy-9")

    def test_http_request_without_signals_never_uses_stdio_fallback(self) -> None:
        ctx = _FakeInjectedContext(
            _FakeRequestContext(_modern_meta(), _FakeRequest({"user-agent": "qa"}))
        )
        events = self._run_tool(ctx)
        tool_call = next(event for event in events if event["kind"] == "tool_call")
        self.assertIsNone(tool_call["session_id_hint"])

    def test_stdio_request_keeps_process_scoped_fallback(self) -> None:
        ctx = _FakeInjectedContext(_FakeRequestContext(_modern_meta(), None))
        events = self._run_tool(ctx)
        tool_call = next(event for event in events if event["kind"] == "tool_call")
        self.assertTrue(str(tool_call["session_id_hint"]).startswith("stdio-"))

    def test_client_info_absent_yields_unknown_client_without_crash(self) -> None:
        ctx = _FakeInjectedContext(
            _FakeRequestContext(
                _modern_meta(baggage="gen_ai.conversation.id=conv-anon"),
                _FakeRequest({}),
            )
        )
        events = self._run_tool(ctx)
        session_init = next(event for event in events if event["kind"] == "session_init")
        self.assertIsNone(session_init["metadata"]["client_name"])
        self.assertEqual(session_init["metadata"]["protocol_version"], "2026-07-28")

    def test_oversized_request_meta_truncated_in_event(self) -> None:
        ctx = _FakeInjectedContext(
            _FakeRequestContext(
                _modern_meta(
                    baggage="gen_ai.conversation.id=conv-big",
                    extra={"blob": "y" * (MAX_REQUEST_META_BYTES * 2)},
                ),
                _FakeRequest({}),
            )
        )
        events = self._run_tool(ctx)
        tool_call = next(event for event in events if event["kind"] == "tool_call")
        self.assertTrue(tool_call["metadata"]["request_meta_truncated"])
        self.assertTrue(
            str(tool_call["metadata"]["request_meta"]).endswith(REQUEST_META_TRUNCATION_MARKER)
        )


class Mcp2GuardWarningTests(unittest.TestCase):
    """The v1-path degradation guard under mcp>=2.

    The version probe is forced so the guard's behavior is pinned in every
    environment, not only when mcp 2.x happens to be installed.
    """

    def setUp(self) -> None:
        self._saved_major = sdk_v2._installed_mcp_major
        sdk_v2._installed_mcp_major = 2
        sdk_v2._reset_warnings_for_tests()

    def tearDown(self) -> None:
        sdk_v2._installed_mcp_major = self._saved_major
        sdk_v2._reset_warnings_for_tests()

    def test_tool_call_without_any_context_warns_and_still_works(self) -> None:
        batches: list = []
        mcp = _FakeFastMCP()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            instrumentation = instrument_fastmcp(
                mcp,
                {
                    "armature": {
                        "delivery": "await",
                        "request_capability": False,
                        "actor_id": "guard-actor",
                        "emit": batches.append,
                    }
                },
            )

        @mcp.tool(name="lookup")
        def lookup(customer_id: str) -> dict:
            return {"customer_id": customer_id}

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertLogs("armature_mcp_analytics", level="WARNING") as logs:
                result = asyncio.run(lookup(customer_id="c1"))
        self.assertEqual(result, {"customer_id": "c1"})
        self.assertTrue(
            any("session attribution will degrade" in str(warning.message) for warning in caught),
            [str(warning.message) for warning in caught],
        )
        self.assertTrue(any("session attribution will degrade" in line for line in logs.output))
        asyncio.run(instrumentation.recorder.flush())
        self.assertTrue(batches, "the guard must warn, never break delivery")

    def test_guard_warns_once_per_process(self) -> None:
        mcp = _FakeFastMCP()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            instrument_fastmcp(
                mcp,
                {
                    "armature": {
                        "delivery": "await",
                        "request_capability": False,
                        "actor_id": "guard-actor",
                        "emit": lambda batch: None,
                    }
                },
            )

        @mcp.tool(name="lookup")
        def lookup(customer_id: str) -> dict:
            return {"customer_id": customer_id}

        with warnings.catch_warnings(record=True) as first:
            warnings.simplefilter("always")
            asyncio.run(lookup(customer_id="c1"))
        with warnings.catch_warnings(record=True) as second:
            warnings.simplefilter("always")
            asyncio.run(lookup(customer_id="c2"))
        gap = "session attribution will degrade"
        self.assertTrue(any(gap in str(warning.message) for warning in first))
        self.assertFalse(any(gap in str(warning.message) for warning in second))

    def test_unknown_server_warns_at_instrument_time(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            instrument_fastmcp(
                _FakeFastMCP(),
                {"armature": {"delivery": "await", "request_capability": False, "emit": lambda batch: None}},
            )
        self.assertTrue(
            any("not a recognized SDK v2 surface" in str(warning.message) for warning in caught),
            [str(warning.message) for warning in caught],
        )

    def test_injected_context_disarms_the_call_time_guard(self) -> None:
        batches: list = []
        mcp = _FakeFastMCP()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            instrument_fastmcp(
                mcp,
                {
                    "armature": {
                        "delivery": "await",
                        "request_capability": False,
                        "actor_id": "guard-actor",
                        "emit": batches.append,
                    }
                },
            )

        @mcp.tool(name="lookup")
        def lookup(customer_id: str) -> dict:
            return {"customer_id": customer_id}

        ctx = _FakeInjectedContext(_FakeRequestContext(_modern_meta(), _FakeRequest({})))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            asyncio.run(lookup(customer_id="c1", ctx=ctx))
        self.assertFalse(
            any("session attribution will degrade" in str(warning.message) for warning in caught),
            [str(warning.message) for warning in caught],
        )


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@unittest.skipUnless(HAVE_MCP2_SERVER, "mcp>=2 (mcp.server.mcpserver) is required")
class OfficialSdkV2Tests(unittest.TestCase):
    def _instrumented_server(self, batches: list) -> "MCPServer":
        mcp = MCPServer("v2-under-test")
        instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "request_capability": False,
                    "actor_id": "official-v2-actor",
                    "emit": batches.append,
                }
            },
        )

        @mcp.tool()
        def lookup_customer(customer_id: str) -> dict:
            """Look up a customer."""
            return {"customer_id": customer_id}

        return mcp

    def test_advertised_schema_carries_telemetry_but_not_the_context_param(self) -> None:
        batches: list = []
        mcp = self._instrumented_server(batches)

        async def check() -> None:
            tools = await mcp.list_tools()
            tool = next(t for t in tools if t.name == "lookup_customer")
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
            properties = schema.get("properties", {})
            self.assertIn("telemetry", properties)
            self.assertIn("user_intent", properties["telemetry"].get("properties", {}))
            self.assertNotIn(ARMATURE_CTX_KWARG, properties)
            self.assertIn("telemetry.user_intent", tool.description or "")

        asyncio.run(check())

    def test_dual_era_http_end_to_end(self) -> None:
        import uvicorn

        batches: list = []
        mcp = self._instrumented_server(batches)
        app = mcp.streamable_http_app(stateless_http=True, json_response=True)
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 10
            while not server.started:
                self.assertLess(time.time(), deadline, "uvicorn failed to start")
                time.sleep(0.05)

            def call(baggage: str) -> dict:
                body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "lookup_customer",
                        "arguments": {
                            "customer_id": "c1",
                            "telemetry": {"user_intent": "modern era intent"},
                        },
                        "_meta": _modern_meta(
                            client={"name": "qa-modern", "version": "9.9.9"},
                            baggage=baggage,
                            extra={"traceparent": "00-abc-def-01"},
                        ),
                    },
                }
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/mcp",
                    data=json.dumps(body).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2026-07-28",
                        "Mcp-Method": "tools/call",
                        "Mcp-Name": "lookup_customer",
                    },
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.loads(response.read())

            first = call("gen_ai.conversation.id=conv-e2e")
            second = call("gen_ai.conversation.id=conv-e2e")
            for payload in (first, second):
                self.assertFalse(payload["result"]["isError"], payload)
        finally:
            server.should_exit = True
            thread.join(timeout=10)

        events = [event for batch in batches for event in batch["events"]]
        session_inits = [event for event in events if event["kind"] == "session_init"]
        tool_calls = [event for event in events if event["kind"] == "tool_call"]
        # session_init on first sight of the session key only.
        self.assertEqual(len(session_inits), 1)
        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(session_inits[0]["session_id_hint"], "conv-e2e")
        self.assertEqual(session_inits[0]["metadata"]["client_name"], "qa-modern")
        self.assertEqual(session_inits[0]["metadata"]["client_version"], "9.9.9")
        self.assertEqual(session_inits[0]["metadata"]["protocol_version"], "2026-07-28")
        for event in tool_calls:
            self.assertEqual(event["session_id_hint"], "conv-e2e")
            self.assertEqual(event["metadata"]["user_intent"], "modern era intent")
            request_meta = event["metadata"]["request_meta"]
            self.assertEqual(request_meta["traceparent"], "00-abc-def-01")
            self.assertEqual(request_meta[CLIENT_INFO_KEY], {"name": "qa-modern", "version": "9.9.9"})
            # The wrapper-injected context parameter must never leak into
            # recorded inputs.
            self.assertNotIn(ARMATURE_CTX_KWARG, event["script_source"])


@unittest.skipUnless(HAVE_FASTMCP4, "fastmcp>=4 is required")
class FastMCP4Tests(unittest.TestCase):
    def test_instrument_and_call_through_client(self) -> None:
        from fastmcp import Client, FastMCP

        batches: list = []
        mcp = FastMCP("fastmcp4-under-test")
        instrumentation = instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "request_capability": False,
                    "actor_id": "fastmcp4-actor",
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
                self.assertEqual(set(tools), {"lookup_customer", "named_lookup"})
                for tool in tools.values():
                    schema = getattr(tool, "input_schema", None) or tool.inputSchema
                    self.assertIn("telemetry", schema.get("properties", {}))
                await client.call_tool(
                    "lookup_customer",
                    {"customer_id": "c1", "telemetry": {"user_intent": "fastmcp4 intent"}},
                )

        asyncio.run(run())
        asyncio.run(instrumentation.recorder.flush())
        tool_calls = [
            event
            for batch in batches
            for event in batch["events"]
            if event["kind"] == "tool_call"
        ]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["metadata"]["user_intent"], "fastmcp4 intent")

    def test_ambient_request_context_supplies_meta_and_session(self) -> None:
        from fastmcp.server.dependencies import FastMCPRequestContext, fastmcp_request_ctx

        batches: list = []
        mcp = _FakeFastMCP()
        instrumentation = instrument_fastmcp(
            mcp,
            {
                "armature": {
                    "delivery": "await",
                    "request_capability": False,
                    "actor_id": "fastmcp4-ambient-actor",
                    "emit": batches.append,
                }
            },
        )

        @mcp.tool(name="lookup")
        def lookup(customer_id: str) -> dict:
            return {"customer_id": customer_id}

        wrapper = FastMCPRequestContext(
            session=None,
            request_id="1",
            meta=_modern_meta(
                client={"name": "ambient-client", "version": "4.0"},
                baggage="gen_ai.conversation.id=conv-ambient",
            ),
            request=_FakeRequest({"user-agent": "qa"}),
            protocol_version="2026-07-28",
            close_sse_stream=None,
            lifespan_context=None,
            _srctx=None,
        )
        token = fastmcp_request_ctx.set(wrapper)
        try:
            asyncio.run(lookup(customer_id="c1"))
        finally:
            fastmcp_request_ctx.reset(token)
        asyncio.run(instrumentation.recorder.flush())
        events = [event for batch in batches for event in batch["events"]]
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event["session_id_hint"], "conv-ambient")
        session_init = next(event for event in events if event["kind"] == "session_init")
        self.assertEqual(session_init["metadata"]["client_name"], "ambient-client")


if __name__ == "__main__":
    unittest.main()
