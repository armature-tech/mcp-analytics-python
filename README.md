# armature-mcp-analytics

[Armature](https://armature.tech) analytics for Python MCP servers. Drop in
the FastMCP wrapper, keep writing normal tools, and get Armature events for who
called each tool, what the agent was trying to do, and where calls failed.

The Python SDK is FastMCP-first and supports both common import paths:

- `from fastmcp import FastMCP`
- `from mcp.server.fastmcp import FastMCP`

It also exposes lower-level recorder and dispatcher primitives for custom MCP
servers.

## Getting Started

**Cloud:** sign in at [app.armature.tech](https://app.armature.tech), create a
server, and copy the ingest API key.

**Install the SDK** in your MCP server environment:

```bash
pip install "armature-mcp-analytics[fastmcp]"
```

Use the `mcp` extra instead if your server imports FastMCP from the official MCP
Python SDK:

```bash
pip install "armature-mcp-analytics[mcp]"
```

Install both extras when a repo supports either import path:

```bash
pip install "armature-mcp-analytics[fastmcp,mcp]"
```

**Wrap your FastMCP server before registering tools:**

```python
import os
from fastmcp import FastMCP
from armature_mcp_analytics import instrument_fastmcp

mcp = FastMCP("Customer MCP")

analytics = instrument_fastmcp(
    mcp,
    {
        "armature": {
            # endpoint_url / api_key default to env vars
            "api_key": os.getenv("ANALYTICS_INGEST_API_KEY"),
            "delivery": "await",
        }
    },
)


@mcp.tool
def lookup_customer(customer_id: str) -> dict:
    """Look up a customer by id."""
    return {"customer_id": customer_id, "status": "active"}


if __name__ == "__main__":
    mcp.run()
```

The same wrapper works with the SDK-integrated FastMCP:

```python
from mcp.server.fastmcp import FastMCP
from armature_mcp_analytics import instrument_fastmcp

mcp = FastMCP("Customer MCP")
instrument_fastmcp(mcp, {"armature": {"delivery": "await"}})
```

That's it. Tools registered after `instrument_fastmcp(...)` are decorated and
their calls are recorded.

> Want an agent to wire this into a repo? Point it at
> [`SKILL.md`](SKILL.md). The playbook tells it how to detect the FastMCP import
> path, where to place the wrapper, and how to verify that telemetry is really
> emitted.

## How It Works

Three things happen on every instrumented tool call:

1. **The agent sees a `telemetry` block** added to the tool input schema with
   `intent`, `context`, and `frustration_level`. The block is optional.
2. **Your handler sees its original args.** The SDK strips `telemetry` before
   invoking your function.
3. **An authenticated batch is POSTed to Armature** with timing, status,
   input/output previews, and whatever telemetry the agent supplied. The first
   call for a session id also emits `session_init`.

Telemetry is observability, not auth. Keep your existing MCP auth/authorization
checks in place.

## Integration Shapes

### FastMCP decorator

Use `instrument_fastmcp(mcp, config)` for servers that use `@mcp.tool` or
`@mcp.tool(...)`. Instrument the server before the tool decorators run:

```python
from fastmcp import FastMCP
from armature_mcp_analytics import instrument_fastmcp

mcp = FastMCP("Orders MCP")
instrument_fastmcp(mcp, {"armature": {"delivery": "await"}})


@mcp.tool(name="lookup_order")
async def lookup_order(order_id: str) -> dict:
    return {"order_id": order_id}
```

`instrument_fastmcp` is idempotent. Calling it twice on the same server returns
the existing instrumentation instead of double-wrapping tools.

### Lower-level recorder / dispatcher

For custom JSON-RPC dispatchers or servers that do not use FastMCP decorators,
use `create_analytics_recorder()`:

```python
from armature_mcp_analytics import create_analytics_recorder

analytics = create_analytics_recorder({"armature": {"delivery": "await"}})


async def lookup_customer(args, context):
    return {"customer_id": args["customer_id"]}


analytics.tool(
    {
        "name": "lookup_customer",
        "description": "Look up a customer by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    lookup_customer,
)

# In tools/list:
tools = analytics.tool_definitions()

# In tools/call:
result = await analytics.dispatch(
    "lookup_customer",
    {"customer_id": "cus_123", "telemetry": {"intent": "find customer"}},
    {"sessionId": "session_123"},
)
```

Pass the MCP session id, request id, headers, auth info, and client info in the
dispatch context when your server has them. That improves session grouping,
actor attribution, and client attribution in Armature.

## Configuration

```python
config = {
    "armature": {
        "endpoint_url": "https://app.armature.tech/api/mcp-analytics/ingest",
        "api_key": "...",
        "actor_id": "stable-user-or-tenant-seed",
        "enabled": True,
        "delivery": "await",  # "background" or "await"
        "timeout_ms": 500,
        "emit": None,         # optional test/custom emitter
        "on_error": None,     # optional delivery error hook
    }
}
```

CamelCase aliases are also accepted for JS parity:
`endpointUrl`, `apiKey`, `actorId`, `timeoutMs`, and `onError`.

**Delivery mode.** `"background"` schedules delivery on the running event loop
and returns the tool result immediately. Use it for long-lived processes and
call `await analytics.recorder.flush()` at shutdown. `"await"` waits for the
batch delivery attempt before returning and is the safer choice for serverless
or short-lived request handlers.

**Actor id.** By default the SDK derives an actor seed from MCP `authInfo`
(`token`, `clientId`, `apiKey`, or `principalId`), then the `Authorization`
header, then `"anonymous"`. Pass `armature.actor_id` as a string or function to
control the seed:

```python
def actor_id(input):
    return input.get("authInfo", {}).get("principalId", "anonymous")


instrument_fastmcp(mcp, {"armature": {"actor_id": actor_id}})
```

**Missing API key.** If no API key is configured, delivery silently no-ops. This
is intentional for local development.

**Auth.** Each batch is POSTed with `Authorization: Bearer <api_key>`. Server
identity is resolved from the API key.

## Environment Variables

| Variable | Purpose |
| --- | --- |
| `ANALYTICS_INGEST_API_KEY` | Armature ingest API key. Missing keys no-op for local development. |
| `ANALYTICS_INGEST_URL` | Optional ingest endpoint override. Defaults to `https://app.armature.tech/api/mcp-analytics/ingest`. |

## Verification

Do both checks when installing the SDK into a server:

1. **Schema decoration:** start the MCP server or call its tool-listing helper
   and confirm at least one tool has `telemetry` in its input schema.
2. **Batch emission:** configure `armature.emit` to capture a batch, invoke a
   tool with `{"telemetry": {"intent": "test"}}`, and assert a `tool_call`
   event is captured with that intent.

Example local capture:

```python
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
        {"message": "hello", "telemetry": {"intent": "verify analytics"}},
    )
    await instrumentation.recorder.flush()
    assert batches[0]["events"][0]["kind"] == "tool_call"
    assert batches[0]["events"][0]["metadata"]["intent"] == "verify analytics"


asyncio.run(main())
```

A passing import or type check is not enough; verify both schema decoration and
batch emission.

## Python vs. JavaScript SDK

The Python SDK covers the most common Python MCP framework path today:
FastMCP, including both the standalone `fastmcp` package and the official MCP
SDK import path. It also includes recorder/dispatcher primitives for custom
servers.

The JavaScript SDK currently has additional adapters for JS-specific shapes,
including Mastra and stateless HTTP helpers. Those do not apply directly to
Python. If your Python server has a custom stateless HTTP transport, pass stable
`sessionId`, `clientInfo`, headers, and auth info into the recorder/dispatcher
context yourself so Armature can group sessions correctly.

## More

- **Official MCP SDK support:** install with `pip install "armature-mcp-analytics[mcp]"`
  and use `from mcp.server.fastmcp import FastMCP`.
- **Custom integrations:** use `create_analytics_recorder`,
  `decorate_input_schema_with_telemetry`, and `extract_telemetry_arguments` for
  non-FastMCP servers.
- **Support:** `hey@armature.tech` or open an issue.
