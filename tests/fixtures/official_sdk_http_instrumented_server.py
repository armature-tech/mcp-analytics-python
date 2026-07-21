"""Official-SDK Streamable HTTP fixture for the transport-level e2e suite.

The launch-blocking shape from production QA: a server built on the official
MCP Python SDK's FastMCP (``mcp.server.fastmcp``) with the standalone
``fastmcp`` package NOT installed, serving many concurrent MCP sessions from
one long-lived process behind ``StatelessHttpSessionMiddleware``. Header
resolution must come from the official SDK's ``request_ctx`` — without it,
every request degraded to the process-scoped ``stdio-`` session id and all
concurrent conversations merged into one session with a null client.

Telemetry is posted over HTTP to the sink the test passes via env.
"""
from __future__ import annotations

import os
import sys


class _BlockStandaloneFastmcp:
    """Make `import fastmcp` fail even when the package is installed.

    CI installs the standalone fastmcp for the rest of the suite; this fixture
    must reproduce the environment where only the official SDK exists.
    """

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fastmcp" or fullname.startswith("fastmcp."):
            raise ImportError("standalone fastmcp is blocked in this fixture")
        return None


sys.meta_path.insert(0, _BlockStandaloneFastmcp())

import uvicorn  # noqa: E402 - after the import blocker, deliberately
from mcp.server.fastmcp import FastMCP  # noqa: E402

from armature_mcp_analytics import (  # noqa: E402
    StatelessHttpSessionMiddleware,
    instrument_fastmcp,
)

endpoint_url = os.environ["ANALYTICS_INGEST_URL"]
api_key = os.environ["ANALYTICS_INGEST_API_KEY"]
port = int(os.environ["FIXTURE_HTTP_PORT"])

mcp = FastMCP("e2e-official-sdk-http-fixture", stateless_http=True)
# Instrument BEFORE registering tools: the adapter patches `mcp.tool`.
instrument_fastmcp(
    mcp,
    {"armature": {"endpoint_url": endpoint_url, "api_key": api_key, "delivery": "await"}},
)


@mcp.tool()
def echo(message: str) -> dict:
    return {"message": message}


if __name__ == "__main__":
    app = StatelessHttpSessionMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
