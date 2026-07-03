"""Real stdio fixture for the transport-level e2e suite.

An instrumented FastMCP server run over actual stdio, spawned as a child
process by tests/test_e2e_stdio.py the same way `claude -p` spawns MCP
servers. Telemetry is posted over HTTP to the sink the test passes via env —
no emit hook can cross the process boundary, which is exactly the blind spot
that let the null-session_id_hint bug ship.
"""
from __future__ import annotations

import os

from fastmcp import FastMCP

from armature_mcp_analytics import instrument_fastmcp

endpoint_url = os.environ["ANALYTICS_INGEST_URL"]
api_key = os.environ["ANALYTICS_INGEST_API_KEY"]

mcp = FastMCP("e2e-stdio-fixture")
# Instrument BEFORE registering tools: the adapter patches `mcp.tool`.
# delivery "await" makes each tool-call response wait for its telemetry POST,
# so the test can assert on the sink as soon as call_tool returns.
instrument_fastmcp(
    mcp,
    {"armature": {"endpoint_url": endpoint_url, "api_key": api_key, "delivery": "await"}},
)


@mcp.tool
def echo(message: str) -> dict:
    return {"message": message}


if __name__ == "__main__":
    mcp.run()
