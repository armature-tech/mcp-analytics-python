"""Transport-level e2e for the Python SDK over real Streamable HTTP.

Companion to test_e2e_stdio.py, covering the opposite edge of the stdio
session fix: on an HTTP server, MANY sessions share one long-lived process,
so the process-scoped `stdio-` fallback must never fire. The adapter keeps it
out by surfacing request headers via fastmcp's `get_http_headers()` — this
test pins that guard end to end: an instrumented FastMCP fixture is spawned
as a child process serving real HTTP, two client sessions run against it, and
the events arriving at an in-test ingest sink must carry the two distinct
transport-issued `Mcp-Session-Id`s.

Requires the optional `fastmcp` extra (which brings the `mcp` client); the
suite skips itself when either is missing so the plain unit run stays green.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    HAVE_MCP_CLIENT = True
except Exception:  # pragma: no cover - absence is a valid environment
    HAVE_MCP_CLIENT = False

try:
    import fastmcp  # noqa: F401

    HAVE_FASTMCP = True
except Exception:  # pragma: no cover - absence is a valid environment
    HAVE_FASTMCP = False

# Discovery imports these modules as `tests.test_e2e_http` when run from the
# package dir but as top-level `test_e2e_http` when run from the repo root
# (root `check:mcp-analytics-python` script); support both.
try:
    from .test_e2e_stdio import INGEST_API_KEY, PACKAGE_ROOT, _IngestSink
except ImportError:
    from test_e2e_stdio import INGEST_API_KEY, PACKAGE_ROOT, _IngestSink

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "http_instrumented_server.py"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_serving(url: str, process: subprocess.Popen, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"HTTP fixture exited early with code {process.returncode}")
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return
        except urllib.error.HTTPError:
            # Any HTTP response (405/406/...) means the server is up.
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"HTTP fixture did not start serving {url} within {timeout_s}s")


async def _run_http_conversation(mcp_url: str, message: str) -> str:
    """One client session over real Streamable HTTP; returns the
    transport-issued session id."""
    async with streamablehttp_client(mcp_url) as (read, write, get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("echo", {"message": message})
            self_check = json.loads(result.content[0].text)
            assert self_check == {"message": message}, self_check
            session_id = get_session_id()
            assert session_id, "server must issue an Mcp-Session-Id"
            return session_id


@unittest.skipUnless(
    HAVE_MCP_CLIENT and HAVE_FASTMCP,
    "fastmcp + mcp are required for the HTTP transport e2e suite",
)
class E2EHttpTransportTests(unittest.TestCase):
    def test_two_http_sessions_on_one_process_keep_their_transport_ids(self) -> None:
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

            first_id = asyncio.run(_run_http_conversation(mcp_url, "first http session"))
            second_id = asyncio.run(_run_http_conversation(mcp_url, "second http session"))
            self.assertNotEqual(first_id, second_id)

            events = [event for request in sink.requests for event in request["batch"]["events"]]
            session_inits = [event for event in events if event["kind"] == "session_init"]
            tool_calls = [event for event in events if event["kind"] == "tool_call"]
            self.assertEqual(len(tool_calls), 2)
            self.assertEqual(len(session_inits), 2, "one session_init per HTTP session")

            # Hints are exactly the transport-issued ids — never the stdio
            # process-scoped fallback, even though both sessions share one
            # server process. This is the get_http_headers() guard, end to end.
            hints = {event["session_id_hint"] for event in events}
            self.assertEqual(hints, {first_id, second_id})
            for hint in hints:
                self.assertFalse(str(hint).startswith("stdio-"), "stdio fallback leaked into HTTP")
            self.assertEqual(len({event["actor_id"] for event in events}), 1)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            sink.close()


if __name__ == "__main__":
    unittest.main()
