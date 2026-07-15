# Armature MCP Analytics for Python

Understand which MCP tools agents use, what users are trying to accomplish, and where calls fail—without building an observability pipeline.

[![PyPI version](https://img.shields.io/pypi/v/armature-mcp-analytics?label=PyPI)](https://pypi.org/project/armature-mcp-analytics/)
[![Python versions](https://img.shields.io/pypi/pyversions/armature-mcp-analytics)](https://pypi.org/project/armature-mcp-analytics/)
[![CI](https://github.com/armature-tech/mcp-analytics-python/actions/workflows/ci.yml/badge.svg)](https://github.com/armature-tech/mcp-analytics-python/actions/workflows/ci.yml)
[![Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

[Armature](https://armature.tech) · [TypeScript SDK](https://github.com/armature-tech/mcp-analytics) · [Go SDK](https://github.com/armature-tech/mcp-analytics-go) · [Agent install](SKILL.md)

## Install in 30 seconds

### 1. Install

For servers using the standalone FastMCP package:

~~~bash
pip install "armature-mcp-analytics[fastmcp]"
~~~

If FastMCP comes from the official MCP Python SDK:

~~~bash
pip install "armature-mcp-analytics[mcp]"
~~~

### 2. Add your ingest key

Create a server in the [Armature dashboard](https://app.armature.tech), copy its ingest key, and add it to your environment:

~~~bash
export ANALYTICS_INGEST_API_KEY="..."
~~~

### 3. Instrument FastMCP

Call **instrument_fastmcp** before registering your tools:

~~~python
from fastmcp import FastMCP
from armature_mcp_analytics import instrument_fastmcp

mcp = FastMCP("Customer MCP")

instrument_fastmcp(
    mcp,
    {"armature": {"delivery": "await"}},
)


@mcp.tool
def lookup_customer(customer_id: str) -> dict:
    return {
        "customer_id": customer_id,
        "status": "active",
    }


mcp.run()
~~~

> **That’s it. Make one tool call, open Armature, and the session is already there.**

## Built for MCP—not page views

| Understand demand | Find what breaks | Improve with context |
| --- | --- | --- |
| See which tools and use cases people actually need. | Surface failures, retries, latency, and dead ends. | Connect every call to user intent and agent reasoning. |

No custom event schema. No logging pipeline. No changes to your tool handlers.

## What you see in Armature

- Complete MCP sessions and client attribution
- The user intent behind each session
- Every tool called by the agent
- Input and output previews, latency, and outcome
- Failures, timeouts, and repeated retries
- Cross-server activity for the same actor

## How it works

Armature instruments the boundary around every tool call:

1. The SDK adds an optional **telemetry** block to the tool’s input schema.
2. The agent can attach user intent, reasoning, and frustration to the call.
3. The SDK removes telemetry before your handler receives the arguments.
4. Timing, outcome, and truncated previews are sent to your dashboard.

~~~json
{
  "telemetry": {
    "user_turn": 1,
    "user_intent": "Check whether the customer's last payment succeeded",
    "agent_thinking": "The payment lookup tool provides the requested status",
    "user_frustration": "low"
  }
}
~~~

All telemetry fields are optional. The earlier **intent**, **context**, and **frustration_level** names remain accepted for clients with cached schemas.

> **Privacy:** Armature is observability, not authentication. Keep your existing MCP authentication and authorization in place. Do not put secrets in tool arguments or telemetry fields.

## Supported Python MCP servers

| Your server | Install | Integration |
| --- | --- | --- |
| **from fastmcp import FastMCP** | **armature-mcp-analytics[fastmcp]** | **instrument_fastmcp(...)** |
| **from mcp.server.fastmcp import FastMCP** | **armature-mcp-analytics[mcp]** | **instrument_fastmcp(...)** |
| Custom dispatcher | Base package | **create_analytics_recorder(...)** |

The FastMCP wrapper is idempotent. Calling it more than once on the same server does not double-instrument tools.

### Official MCP Python SDK

~~~python
from mcp.server.fastmcp import FastMCP
from armature_mcp_analytics import instrument_fastmcp

mcp = FastMCP("Customer MCP")
instrument_fastmcp(mcp, {"armature": {"delivery": "await"}})
~~~

### Custom dispatcher

Use the recorder when you manage **tools/list** and **tools/call** yourself:

~~~python
from armature_mcp_analytics import create_analytics_recorder

analytics = create_analytics_recorder(
    {"armature": {"delivery": "await"}}
)


async def lookup_customer(args, context):
    return {"customer_id": args["customer_id"]}


analytics.tool(
    {
        "name": "lookup_customer",
        "description": "Look up a customer by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
            },
            "required": ["customer_id"],
        },
    },
    lookup_customer,
)

# tools/list
tools = analytics.tool_definitions()

# tools/call
result = await analytics.dispatch(
    "lookup_customer",
    {
        "customer_id": "cus_123",
        "telemetry": {
            "user_intent": "Find the customer",
        },
    },
    {"sessionId": "session_123"},
)
~~~

Pass stable session, client, request, header, and authentication information in the dispatcher context when it is available.

## Let your coding agent install it

Point Claude Code, Cursor, or Codex at [SKILL.md](SKILL.md), then ask:

> Install Armature MCP Analytics using the repository’s SKILL.md. Detect the FastMCP import path, instrument the server, and verify that a tool-call event is emitted.

The playbook covers both FastMCP import paths and custom dispatchers.

## Configuration

Most servers only need **ANALYTICS_INGEST_API_KEY**. Operational controls are available when you need them:

~~~python
instrumentation = instrument_fastmcp(
    mcp,
    {
        "armature": {
            "endpoint_url": "https://app.armature.tech/api/mcp-analytics/ingest",
            "api_key": "...",
            "actor_id": "stable-user-or-tenant-seed",
            "enabled": True,
            "delivery": "await",
            "timeout_ms": 500,
            "emit": None,
            "on_error": None,
        }
    },
)
~~~

| Option | Default | Purpose |
| --- | --- | --- |
| **endpoint_url** | Armature cloud | Override the ingestion endpoint |
| **api_key** | **ANALYTICS_INGEST_API_KEY** | Authenticate events and identify the MCP server |
| **actor_id** | Derived from request auth | Supply a stable user or tenant seed |
| **enabled** | **True** | Enable or disable instrumentation |
| **delivery** | **"background"** | Use **"await"** for serverless or short-lived processes |
| **timeout_ms** | **500** | Set the delivery timeout |
| **emit** | Network emitter | Replace delivery for tests or custom pipelines |
| **on_error** | None | Observe delivery failures |

CamelCase aliases such as **endpointUrl**, **apiKey**, **actorId**, **timeoutMs**, and **onError** are accepted for JavaScript parity.

### Delivery

- **"background"** schedules delivery on the running event loop. Call **await instrumentation.recorder.flush()** during shutdown.
- **"await"** waits for the delivery attempt before returning. Use it for serverless functions and short-lived processes.

If the API key is missing, delivery quietly no-ops for local development.

### Actor identification

By default, the SDK derives an actor seed from MCP authentication information or the Authorization header. You can provide a string or function through **actor_id**:

~~~python
def actor_id(context):
    return context.get("authInfo", {}).get("principalId", "anonymous")


instrument_fastmcp(
    mcp,
    {"armature": {"actor_id": actor_id}},
)
~~~

The seed is hashed before transmission. Armature scopes the resulting actor identifier to your server.

## Verify your integration

A successful import is not enough. Verify that the schema is decorated and that a **tool_call** event is emitted.

Replace network delivery with a local capture:

~~~python
import asyncio

from fastmcp import FastMCP
from armature_mcp_analytics import instrument_fastmcp

batches = []
mcp = FastMCP("Analytics smoke test")

instrumentation = instrument_fastmcp(
    mcp,
    {
        "armature": {
            "delivery": "await",
            "actor_id": "smoke-test",
            "emit": batches.append,
        }
    },
)


@mcp.tool
def ping(message: str) -> dict:
    return {"message": message}


async def main():
    await mcp.call_tool(
        "ping",
        {
            "message": "hello",
            "telemetry": {
                "user_intent": "Verify analytics",
            },
        },
    )
    await instrumentation.recorder.flush()

    event = next(
        event
        for batch in batches
        for event in batch["events"]
        if event["kind"] == "tool_call"
    )
    assert event["metadata"]["user_intent"] == "Verify analytics"


asyncio.run(main())
~~~

## Compatibility

- Python 3.10+
- FastMCP 2.x and 3.x
- Official MCP Python SDK 1.27+
- Synchronous and asynchronous tool handlers

## Environment variables

| Variable | Purpose |
| --- | --- |
| **ANALYTICS_INGEST_API_KEY** | Armature ingest key |
| **ANALYTICS_INGEST_URL** | Optional ingestion endpoint override |

## Example

Run the complete stdio server in [examples/minimal](examples/minimal):

~~~bash
cd examples/minimal
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ANALYTICS_INGEST_API_KEY="..." python server.py
~~~

## Support

[Open an issue](https://github.com/armature-tech/mcp-analytics-python/issues) · [Email us](mailto:hey@armature.tech) · [Releases](https://github.com/armature-tech/mcp-analytics-python/releases)

## License

Licensed under the [Apache License 2.0](LICENSE).
