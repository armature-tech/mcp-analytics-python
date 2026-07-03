"""Transport-level e2e for the Python SDK.

The unit suite drives the recorder with hand-built context dicts, which is
exactly how the null-session_id_hint bug shipped — nothing ever exercised
what a REAL stdio transport hands the SDK. This test spawns the instrumented
FastMCP fixture as a child process over actual stdio (the `claude -p` shape),
drives it with a real MCP client, and asserts on the payloads that arrive at
an in-test HTTP ingest sink.

Requires the optional `fastmcp` extra (which brings the `mcp` client); the
suite skips itself when either is missing so the plain unit run stays green.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    HAVE_MCP_CLIENT = True
except Exception:  # pragma: no cover - absence is a valid environment
    HAVE_MCP_CLIENT = False

try:
    import fastmcp  # noqa: F401

    HAVE_FASTMCP = True
except Exception:  # pragma: no cover - absence is a valid environment
    HAVE_FASTMCP = False

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "stdio_instrumented_server.py"
INGEST_API_KEY = "e2e-test-ingest-key"


class _IngestSink:
    """Minimal in-test stand-in for POST /api/mcp-analytics/ingest."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length)
                sink.requests.append(
                    {
                        "authorization": self.headers.get("authorization"),
                        "batch": json.loads(body),
                    }
                )
                payload = json.dumps({"accepted": 1, "rejected": []}).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/ingest"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


async def _run_cli_conversation(sink_url: str, message: str) -> None:
    """One simulated `claude -p` conversation: spawn the fixture as a child
    process over real stdio, initialize, call one tool, disconnect."""
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(FIXTURE)],
        env={
            **os.environ,
            "PYTHONPATH": str(PACKAGE_ROOT / "src"),
            "ANALYTICS_INGEST_URL": sink_url,
            "ANALYTICS_INGEST_API_KEY": INGEST_API_KEY,
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("echo", {"message": message})
            self_check = json.loads(result.content[0].text)
            assert self_check == {"message": message}, self_check


@unittest.skipUnless(
    HAVE_MCP_CLIENT and HAVE_FASTMCP,
    "fastmcp + mcp are required for the stdio transport e2e suite",
)
class E2EStdioTransportTests(unittest.TestCase):
    def test_two_cli_conversations_reach_ingest_as_two_distinct_sessions(self) -> None:
        sink = _IngestSink()
        try:
            asyncio.run(_run_cli_conversation(sink.url, "first run"))
            asyncio.run(_run_cli_conversation(sink.url, "second run"))

            # The fixture authenticates like a real deployment.
            for request in sink.requests:
                self.assertEqual(request["authorization"], f"Bearer {INGEST_API_KEY}")

            events = [event for request in sink.requests for event in request["batch"]["events"]]
            session_inits = [event for event in events if event["kind"] == "session_init"]
            tool_calls = [event for event in events if event["kind"] == "tool_call"]
            self.assertEqual(len(session_inits), 2, "each stdio conversation emits one session_init")
            self.assertEqual(len(tool_calls), 2)

            # The regression itself: every event must carry a session id...
            for event in events:
                self.assertIsInstance(
                    event["session_id_hint"], str, f"{event['kind']} shipped a null hint"
                )
            # ...both conversations came from the same (anonymous) actor,
            # exactly the merge scenario from production...
            self.assertEqual(len({event["actor_id"] for event in events}), 1)
            # ...and yet they are distinguishable: one id per process, two in
            # total.
            hints = {event["session_id_hint"] for event in events}
            self.assertEqual(len(hints), 2, "two CLI processes must yield two session ids")
        finally:
            sink.close()


if __name__ == "__main__":
    unittest.main()
