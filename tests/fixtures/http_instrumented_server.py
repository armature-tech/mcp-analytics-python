"""Real Streamable HTTP fixture for the transport-level e2e suite.

One long-lived instrumented FastMCP server process serving MANY MCP sessions
over actual HTTP — the shape the stdio process-scoped session fallback must
never leak into. This is the direct end-to-end check on the adapter's
`get_http_headers()` guard: requests carry headers, so events must keep the
transport-issued `Mcp-Session-Id` instead of a `stdio-` fallback.

Telemetry is posted over HTTP to the sink the test passes via env.
"""
from __future__ import annotations

import os

from fastmcp import FastMCP

from armature_mcp_analytics import instrument_fastmcp

endpoint_url = os.environ["ANALYTICS_INGEST_URL"]
api_key = os.environ["ANALYTICS_INGEST_API_KEY"]
port = int(os.environ["FIXTURE_HTTP_PORT"])

mcp = FastMCP("e2e-http-fixture")
# Instrument BEFORE registering tools: the adapter patches `mcp.tool`.
instrument_fastmcp(
    mcp,
    {"armature": {"endpoint_url": endpoint_url, "api_key": api_key, "delivery": "await"}},
)


@mcp.tool
def echo(message: str) -> dict:
    return {"message": message}


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=port)
