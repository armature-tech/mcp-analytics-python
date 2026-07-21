"""Transport-level e2e for official-SDK servers over real Streamable HTTP.

Regression suite for the QA-04 launch blocker: a server importing FastMCP
from the official MCP Python SDK (``mcp.server.fastmcp``) without the
standalone ``fastmcp`` package installed. Header resolution used to depend
exclusively on fastmcp's accessors, so every HTTP request fell through to
the process-scoped ``stdio-`` session id: all concurrent conversations
served by one warm process merged into a single reconstructed session and
``client_name`` was null. The fixture blocks the standalone fastmcp import,
serves behind ``StatelessHttpSessionMiddleware``, and two CONCURRENT client
sessions with distinct client identities must come out the other side as two
sessions with the middleware-issued identity-bearing ids.

Requires the `mcp` package only — the point is that fastmcp is absent.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

try:
    from mcp import ClientSession, types
    from mcp.client.streamable_http import streamablehttp_client

    HAVE_MCP_CLIENT = True
except Exception:  # pragma: no cover - absence is a valid environment
    HAVE_MCP_CLIENT = False

# Discovery imports these modules as `tests.test_...` when run from the
# package dir but as top-level modules when run from the repo root; support
# both (see test_e2e_http.py).
try:
    from .test_e2e_stdio import INGEST_API_KEY, PACKAGE_ROOT, _IngestSink
    from .test_e2e_http import _free_port, _wait_until_serving
except ImportError:
    from test_e2e_stdio import INGEST_API_KEY, PACKAGE_ROOT, _IngestSink
    from test_e2e_http import _free_port, _wait_until_serving

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "official_sdk_http_instrumented_server.py"


async def _run_conversation(mcp_url: str, client_name: str, client_version: str, message: str) -> str:
    """One client session with explicit client identity; returns the
    server-issued session id."""
    async with streamablehttp_client(mcp_url) as (read, write, get_session_id):
        async with ClientSession(
            read,
            write,
            client_info=types.Implementation(name=client_name, version=client_version),
        ) as session:
            await session.initialize()
            result = await session.call_tool("echo", {"message": message})
            self_check = json.loads(result.content[0].text)
            assert self_check == {"message": message}, self_check
            session_id = get_session_id()
            assert session_id, "middleware must issue an Mcp-Session-Id"
            return session_id


async def _run_concurrent_conversations(mcp_url: str) -> tuple[str, str]:
    return await asyncio.gather(
        _run_conversation(mcp_url, "qa-client-one", "1.0.1", "first concurrent conversation"),
        _run_conversation(mcp_url, "qa-client-two", "2.0.2", "second concurrent conversation"),
    )


@unittest.skipUnless(
    HAVE_MCP_CLIENT,
    "the mcp package is required for the official-SDK HTTP e2e suite",
)
class E2EOfficialSdkHttpTests(unittest.TestCase):
    def test_concurrent_official_sdk_sessions_keep_distinct_identity_ids(self) -> None:
        sink = _IngestSink()
        port = _free_port()
        process = subprocess.Popen(
            [sys.executable, str(FIXTURE)],
            env={
                **os.environ,
                "PYTHONPATH": str(PACKAGE_ROOT / "src"),
                "ANALYTICS_INGEST_URL": sink.url,
                "ANALYTICS_INGEST_API_KEY": INGEST_API_KEY,
                "FIXTURE_HTTP_PORT": str(port),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        mcp_url = f"http://127.0.0.1:{port}/mcp"
        try:
            _wait_until_serving(mcp_url, process)

            first_id, second_id = asyncio.run(_run_concurrent_conversations(mcp_url))
            self.assertNotEqual(first_id, second_id)
            # The middleware issues identity-bearing ids minted from the
            # initialize request's clientInfo — not opaque transport UUIDs.
            self.assertTrue(first_id.startswith("mcp_qa-client-one_v_1.0.1_"), first_id)
            self.assertTrue(second_id.startswith("mcp_qa-client-two_v_2.0.2_"), second_id)

            events = [event for request in sink.requests for event in request["batch"]["events"]]
            tool_calls = [event for event in events if event["kind"] == "tool_call"]
            session_inits = [event for event in events if event["kind"] == "session_init"]
            self.assertEqual(len(tool_calls), 2)
            self.assertEqual(len(session_inits), 2, "one session_init per conversation")

            # QA-04: concurrent users are not merged. Every event carries the
            # middleware-issued id of ITS conversation — never the shared
            # process-scoped stdio fallback.
            hints = {event["session_id_hint"] for event in events}
            self.assertEqual(hints, {first_id, second_id})
            for event in events:
                self.assertFalse(
                    str(event["session_id_hint"]).startswith("stdio-"),
                    f"stdio fallback leaked into official-SDK HTTP: {event['session_id_hint']}",
                )

            # Client identity is recovered from the identity-bearing session
            # id, so the dashboard never shows "CLIENT Unknown".
            client_names = {event["metadata"]["client_name"] for event in session_inits}
            self.assertEqual(client_names, {"qa-client-one", "qa-client-two"})
            client_versions = {event["metadata"]["client_version"] for event in session_inits}
            self.assertEqual(client_versions, {"1.0.1", "2.0.2"})
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            sink.close()


if __name__ == "__main__":
    unittest.main()
