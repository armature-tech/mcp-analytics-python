#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
import uuid

from armature_mcp_analytics import create_analytics_recorder


async def main() -> None:
    required = ("SDK_CANARY_INGEST_KEY", "SDK_CANARY_READ_API_KEY", "SDK_CANARY_MCP_SERVER_ID")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"missing live canary configuration: {', '.join(missing)}")
    base = os.environ.get("SDK_CANARY_PLATFORM_URL", "https://app.armature.tech").rstrip("/")
    marker = f"sdk-canary/python/{os.environ.get('GITHUB_RUN_ID', 'manual')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}-{uuid.uuid4()}"
    delivery_errors: list[Exception] = []
    recorder = create_analytics_recorder({"armature": {
        "api_key": os.environ["SDK_CANARY_INGEST_KEY"],
        "endpoint_url": f"{base}/api/mcp-analytics/ingest",
        "delivery": "await",
        "actor_id": "sdk-canary-shared-actor",
        "timeout_ms": 10_000,
        "on_error": lambda error, _batch: delivery_errors.append(error),
    }})
    for label in ("session-a", "session-b"):
        for call, status in (("call-1", "ok"), ("call-2", "error")):
            await recorder.record_tool_call(
                name="canary_echo" if status == "ok" else "canary_expected_error",
                args={"marker": f"{label}/{call}"},
                telemetry={
                    **({"user_intent": marker} if call == "call-1" else {}),
                    "agent_thinking": f"exercise the {status} path",
                },
                session_id=f"{marker}/{label}",
                status=status,
                result={"marker": f"{label}/{call}"} if status == "ok" else None,
                error=None if status == "ok" else "expected canary error",
            )
    await recorder.flush()
    if delivery_errors:
        raise SystemExit(f"platform ingest failed: {delivery_errors[0]}")

    headers = {
        "Authorization": f"Bearer {os.environ['SDK_CANARY_READ_API_KEY']}",
        "Accept": "application/json",
        # Cloudflare blocks Python's default urllib user agent with error 1010.
        "User-Agent": "armature-sdk-canary-python/1.0",
    }
    query = urllib.parse.urlencode({"range": "24h", "intent": marker, "limit": 100})
    deadline = time.monotonic() + 90
    matches = []
    while time.monotonic() < deadline:
        request = urllib.request.Request(f"{base}/api/armature/v1/insights/sessions?{query}", headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            sessions = json.load(response).get("sessions", [])
        matches = [session for session in sessions if session.get("raw_intent") == marker and session.get("mcp_server_id") == os.environ["SDK_CANARY_MCP_SERVER_ID"]]
        if len(matches) == 2:
            break
        await asyncio.sleep(2)
    if len(matches) != 2:
        raise SystemExit(f"expected two platform sessions for {marker}, found {len(matches)}")
    if len({session["session_key"] for session in matches}) != 2 or len({session["actor_id"] for session in matches}) != 1:
        raise SystemExit("platform merged actors or sessions incorrectly")
    for session in matches:
        if (session.get("event_count"), session.get("ok_count"), session.get("error_count")) != (2, 1, 1):
            raise SystemExit(f"unexpected platform counts for {session['id']}: {session}")
        trace_request = urllib.request.Request(
            f"{base}/api/armature/v1/insights/sessions/{session['id']}/trace", headers=headers
        )
        with urllib.request.urlopen(trace_request, timeout=15) as response:
            trace = json.dumps(json.load(response))
        label = "session-a" if "session-a" in trace else "session-b"
        other = "session-b" if label == "session-a" else "session-a"
        if f"{label}/call-1" not in trace or f"{label}/call-2" not in trace or other in trace:
            raise SystemExit(f"cross-session trace contamination in {session['id']}")
        print(f"platform session: {base}/mcp-analytics/sessions/{session['id']}")


asyncio.run(main())
