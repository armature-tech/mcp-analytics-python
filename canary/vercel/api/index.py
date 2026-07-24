from __future__ import annotations

import os
from typing import Any

from armature_mcp_analytics import StatelessHttpSessionMiddleware, instrument_fastmcp
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

PACKAGE_NAME = "python"
DEPLOYMENT = os.getenv("SDK_CANARY_DEPLOYMENT", "unknown")
INTENT = DEPLOYMENT if DEPLOYMENT.startswith("sdk-canary/") else f"sdk-canary/{PACKAGE_NAME}/{DEPLOYMENT}"

mcp = FastMCP("sdk-canary-python", version=DEPLOYMENT)
instrument_fastmcp(
    mcp,
    {
        "armature": {
            "api_key": os.getenv("SDK_CANARY_INGEST_KEY"),
            "endpoint_url": f"{os.getenv('SDK_CANARY_PLATFORM_URL', 'https://app.armature.tech').rstrip('/')}/api/mcp-analytics/ingest",
            "delivery": "await",
            "timeout_ms": 10_000,
            "actor_id": "sdk-canary-browser-worker",
            # The HTTP smoke test asserts an exact tool list; keep the
            # on-by-default request_capability tool out of this fixture.
            "request_capability": False,
        }
    },
)


def _session_id() -> str:
    try:
        headers = get_http_headers(include={"mcp-session-id"})
    except TypeError:
        headers = get_http_headers(include_all=True)
    return str(headers.get("mcp-session-id") or headers.get("Mcp-Session-Id") or "missing")


@mcp.tool(description=f"Call exactly once to get this MCP session identity. Reuse the result; do not retry or call this tool again. Set telemetry.user_intent exactly to {INTENT}.")
def canary_identity() -> dict[str, str]:
    return {
        "package": PACKAGE_NAME,
        "deployment": DEPLOYMENT,
        "session_id": _session_id(),
        "next_step": "Call canary_echo exactly once. Do not call canary_identity again.",
    }


@mcp.tool(description="Call exactly once after canary_identity to echo a marker. Omit telemetry.user_intent because this continues the same user turn.")
def canary_echo(marker: str) -> dict[str, str]:
    return {"marker": marker, "session_id": _session_id(), "deployment": DEPLOYMENT}


class ForceMcpPath:
    """Keep FastMCP mounted at /mcp after Vercel's function rewrite."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            # This test-org-only endpoint must surface workflow-driven traffic
            # in Sessions. Real servers retain this header and correctly hide
            # synthetic workflow sessions from customer analytics.
            workflow_run_id = next(
                (
                    value.decode("ascii")
                    for key, value in scope.get("headers", [])
                    if key.lower() == b"x-armature-workflow-run-id"
                ),
                None,
            )
            headers = [
                (key, value)
                for key, value in scope.get("headers", [])
                if key.lower() != b"x-armature-workflow-run-id"
            ]
            if workflow_run_id:
                headers.append(
                    (b"x-armature-session-seed", workflow_run_id.encode("ascii"))
                )
            scope = {**scope, "path": "/mcp", "raw_path": b"/mcp", "headers": headers}
        await self.inner(scope, receive, send)


app = ForceMcpPath(
    StatelessHttpSessionMiddleware(
        mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=True, json_response=True)
    )
)
