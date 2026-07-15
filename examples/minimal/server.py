"""Minimal stdio MCP server instrumented with Armature."""

from __future__ import annotations

import os

from armature_mcp_analytics import instrument_fastmcp
from fastmcp import FastMCP

if not os.environ.get("ANALYTICS_INGEST_API_KEY"):
    raise RuntimeError("Set ANALYTICS_INGEST_API_KEY before starting the server.")

mcp = FastMCP("armature-minimal-python")
instrument_fastmcp(mcp, {"armature": {"delivery": "await"}})


@mcp.tool
def echo(text: str) -> str:
    """Echo the supplied text."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()
