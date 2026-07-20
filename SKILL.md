---
name: install-armature-mcp-analytics-python
description: >
  Wire the armature-mcp-analytics Python SDK into an existing Python MCP server
  so tool calls emit telemetry to Armature. Use whenever the user wants to add,
  install, integrate, or instrument analytics on a Python MCP server. Detects
  FastMCP import paths from either fastmcp or the official MCP Python SDK,
  chooses the safest delivery mode, edits the right files, and verifies both
  schema decoration and batch emission.
---

# Install armature-mcp-analytics into a Python MCP server

You are integrating the `armature-mcp-analytics` SDK into a customer's Python
MCP server. The SDK decorates each tool's input schema with an optional
`telemetry` block (`user_intent`, `agent_thinking`, `user_frustration`), strips that block
before the handler runs, and posts an authenticated batch to Armature after each
call.

The most common Python shape is FastMCP. Support both import paths:

- `from fastmcp import FastMCP`
- `from mcp.server.fastmcp import FastMCP`

## Step 1: Identify the integration shape

Read enough of the repo to classify it. Grep first; only open the files you
need.

| Signal | Shape |
| --- | --- |
| `from fastmcp import FastMCP`, `import fastmcp`, or `@mcp.tool` on a `fastmcp.FastMCP` instance | **A. FastMCP standalone** |
| `from mcp.server.fastmcp import FastMCP` or `mcp.server.fastmcp.FastMCP` | **B. FastMCP from official MCP Python SDK** |
| Custom `tools/list` and `tools/call` JSON-RPC handlers, no FastMCP decorator | **C. Recorder / dispatcher** |

If the repo has multiple MCP servers, ask the user which one. Do not guess.

## Step 2: Install the dependency

Match the customer's Python packaging tool (`pyproject.toml`, `requirements.txt`,
`uv.lock`, `poetry.lock`, `Pipfile`, etc.).

For standalone FastMCP:

```bash
pip install "armature-mcp-analytics[fastmcp]"
```

For the official MCP Python SDK import path:

```bash
pip install "armature-mcp-analytics[mcp]"
```

If the repo supports both import paths, install both extras:

```bash
pip install "armature-mcp-analytics[fastmcp,mcp]"
```

Do not add both `fastmcp` and `mcp` manually unless the package manager requires
explicit dependencies. Prefer the extras because they encode the tested ranges.

## Step 3: Add the API key environment variable

The SDK needs one credential plus an optional URL override:

| Variable | What it is |
| --- | --- |
| `ANALYTICS_INGEST_API_KEY` | Armature ingest API key. Identifies the MCP server and signs each batch. |
| `ANALYTICS_INGEST_URL` | Optional. Defaults to `https://app.armature.tech/api/mcp-analytics/ingest`. Override for local mock or staging. |

Add `ANALYTICS_INGEST_API_KEY` to the repo's env mechanism (`.env.example`,
Docker/Kubernetes manifests, deployment docs, secret manager config). Do not
commit real secret values. If the key is missing at runtime, the SDK silently
no-ops; that is intentional for local development.

## Step 4: Pick delivery mode

The default is `delivery: "background"`, which schedules delivery on the
running event loop. That can drop batches in short-lived or per-request
serverless handlers.

Use this table:

| Runtime | `delivery` |
| --- | --- |
| Vercel / Lambda / Cloud Run request handlers / short-lived commands | `"await"` |
| Long-lived Python process or container | `"background"` plus `await instrumentation.recorder.flush()` at shutdown |

If you are not sure, choose `"await"`. It is the safer integration default and
only waits for the telemetry delivery attempt.

### Stateless HTTP / serverless sessions

For FastMCP apps deployed without sticky sessions, wrap the ASGI app with
`StatelessHttpSessionMiddleware`:

```python
from armature_mcp_analytics import StatelessHttpSessionMiddleware

app = StatelessHttpSessionMiddleware(
    mcp.http_app(stateless_http=True, json_response=True)
)
```

For `mcp.server.fastmcp.FastMCP`, construct it with `stateless_http=True` and
wrap `mcp.streamable_http_app()`. The middleware issues an identity-bearing
`Mcp-Session-Id` on initialize and recovers that session/client on later cold
requests. Do not deploy stateless HTTP without it; otherwise one conversation
can fragment into one Armature session per call.

## Step 5: Make the edits

### Shape A/B: FastMCP

Instrument the FastMCP instance before tool decorators run. Do not rewrite tool
definitions.

```python
import os
from fastmcp import FastMCP
from armature_mcp_analytics import instrument_fastmcp

mcp = FastMCP("Customer MCP")
instrumentation = instrument_fastmcp(
    mcp,
    {
        "armature": {
            "api_key": os.getenv("ANALYTICS_INGEST_API_KEY"),
            "delivery": "await",
        }
    },
)


@mcp.tool
def lookup_customer(customer_id: str) -> dict:
    return {"customer_id": customer_id}
```

For the official SDK-integrated FastMCP, only the import changes:

```python
from mcp.server.fastmcp import FastMCP
from armature_mcp_analytics import instrument_fastmcp

mcp = FastMCP("Customer MCP")
instrument_fastmcp(mcp, {"armature": {"delivery": "await"}})
```

The wrapper handles both `@mcp.tool` and `@mcp.tool(...)`. It is idempotent, so a
second call on the same server returns the existing instrumentation.

If the server uses `delivery: "background"` in a long-lived process, add a
shutdown hook that calls:

```python
await instrumentation.recorder.flush()
```

Do not put `flush()` inside every tool handler.

### Shape C: Recorder / dispatcher

For servers that publish a custom tool catalog and route `tools/call` manually:

```python
from armature_mcp_analytics import create_analytics_recorder

analytics = create_analytics_recorder({"armature": {"delivery": "await"}})

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
    lambda args, context: {"customer_id": args["customer_id"]},
)

# In tools/list:
return {"tools": analytics.tool_definitions()}

# In tools/call:
return await analytics.dispatch(
    name,
    raw_args,
    {
        "sessionId": session_id,
        "requestId": request_id,
        "headers": headers,
        "authInfo": auth_info,
        "clientInfo": client_info,
    },
)
```

Pass stable session and client context when the server has it. Do not mint a new
random session id on each stateless HTTP request; that makes the dashboard show
one anonymous session per call.

### Optional actor identifier

Use `armature.actor_identifier` to attach any caller-provided non-empty string
up to 8 KiB. The value is hashed into `actor_id`, stored verbatim, and emitted
only when it changes. `actor_id` remains the hashed-only fallback when this is
absent.

## Step 6: Verify the wiring

Two checks. Do not skip them.

**Check 1: schema includes telemetry.** Start the server or call its tool
listing helper. Confirm at least one tool's input schema contains a
`telemetry` property and the tool description mentions `telemetry.user_intent`.

**Check 2: a real tool call produces a batch.** Set `armature.emit` to a stub,
invoke a tool with telemetry, and assert the captured batch has a `tool_call`
event with the right tool name and intent.

Smoke-test pattern:

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
        {"message": "hello", "telemetry": {"user_intent": "verify analytics"}},
    )
    await instrumentation.recorder.flush()
    assert batches[0]["events"][0]["kind"] == "tool_call"
    assert batches[0]["events"][0]["metadata"]["tool_name"] == "ping"
    assert batches[0]["events"][0]["metadata"]["user_intent"] == "verify analytics"


asyncio.run(main())
```

A passing import, type check, or unit test that never calls a tool is not enough.
Verify schema decoration and batch emission.

Also run the language-independent local doctor against the started server:

```bash
npx @armature-tech/mcp-analytics doctor --url http://localhost:3000/mcp
```

Use the same `ANALYTICS_INGEST_API_KEY` and `ANALYTICS_INGEST_URL` as
the Python server. The doctor verifies the MCP handshake, all served tool
schemas, and ingest authentication with an empty batch containing no customer
content. Include its result in the handoff.

## Step 7: Mention the gotchas, then stop

Tell the user briefly:

- Which FastMCP import path you detected.
- Which delivery mode you chose and why.
- Where they must set `ANALYTICS_INGEST_API_KEY`.
- That missing API keys no-op for local development.

## What NOT to do

- Do not expose `ANALYTICS_INGEST_API_KEY` to client-side code.
- Do not rewrite working FastMCP tool definitions into a custom dispatcher.
- Do not add a separate feature flag unless the user asks; `armature.enabled:
  False` already exists and missing API keys no-op.
- Do not wrap analytics calls in noisy `try/except` blocks. The SDK already
  prevents delivery errors from crashing tool handlers and exposes `on_error`
  for custom reporting.
- Do not claim full JavaScript SDK parity. Python currently covers FastMCP plus
  lower-level recorder/dispatcher primitives; JS has extra JS-specific adapters
  such as Mastra.
