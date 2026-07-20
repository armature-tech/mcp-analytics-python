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

### Verify the installation locally

The language-independent doctor can inspect a running Python MCP server:

~~~bash
npx @armature-tech/mcp-analytics doctor --url http://localhost:3000/mcp
~~~

It performs an MCP handshake, verifies every served tool exposes Armature's
telemetry contract, and authenticates the configured ingest key with an empty
batch containing no sessions or customer content. Use `--skip-ingest` for an
offline-only check and `--json` for a machine-readable report.

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
    "user_intent": "Check whether the customer's last payment succeeded",
    "agent_thinking": "The payment lookup tool provides the requested status",
    "user_frustration": "low"
  }
}
~~~

All telemetry fields are optional. Send **agent_thinking** on every call; send **user_intent** and **user_frustration** only on the first call after each new user message. Their absence on later calls means the same turn continues. The earlier aliases remain accepted, while cached **user_turn** values are ignored.

> **Privacy:** Armature is observability, not authentication. Keep your existing MCP authentication and authorization in place. Do not put secrets in tool arguments or telemetry fields.

## Supported Python MCP servers

| Your server | Install | Integration |
| --- | --- | --- |
| **from fastmcp import FastMCP** | **armature-mcp-analytics[fastmcp]** | **instrument_fastmcp(...)** |
| **from mcp.server.fastmcp import FastMCP** | **armature-mcp-analytics[mcp]** | **instrument_fastmcp(...)** |
| Custom dispatcher | Base package | **create_analytics_recorder(...)** |
| Stateless HTTP / serverless | Base package | **StatelessHttpSessionMiddleware(...)** |

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

### Stateless HTTP and serverless

Initialization and tool calls can land on different instances. Wrap a
stateless FastMCP ASGI app so `initialize` issues an identity-bearing session
ID that later requests echo:

~~~python
from armature_mcp_analytics import StatelessHttpSessionMiddleware

# Standalone FastMCP
app = StatelessHttpSessionMiddleware(
    mcp.http_app(stateless_http=True, json_response=True)
)

# Official MCP Python SDK FastMCP
# mcp = FastMCP("Customer MCP", stateless_http=True, json_response=True)
# app = StatelessHttpSessionMiddleware(mcp.streamable_http_app())
~~~

The middleware is dependency-free ASGI. It mints
`mcp_<client>_v_<version>_<uuid>` on a successful initialize response and
preserves the echoed `Mcp-Session-Id` on later cold invocations. The recorder
then recovers the client identity without a session store. Continue to use
`delivery: "await"` in request-scoped deployments.

Custom transports can use the lower-level API directly:

~~~python
from armature_mcp_analytics import resolve_stateless_http_session

session = resolve_stateless_http_session(body=request_body, headers=request_headers)
generator = session.session_id_generator  # initialize only
context = session.dispatch_context         # recorder/dispatcher context
~~~

Session IDs provide observability attribution, not authentication.

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
            "actor_identifier": lambda input: "anything-at-all@example.com",
            "enabled": True,
            "delivery": "await",
            "redact_secrets": True,
            "redact_event": None,
            "schedule": None,
            "timeout_ms": 500,
            "emit": None,
            "on_error": None,
            "request_capability": False,
        }
    },
)
~~~

| Option | Default | Purpose |
| --- | --- | --- |
| **endpoint_url** | Armature cloud | Override the ingestion endpoint |
| **api_key** | **ANALYTICS_INGEST_API_KEY** | Authenticate events and identify the MCP server |
| **actor_id** | Derived from request auth | Supply a stable user or tenant seed |
| **actor_identifier** | None | Store a caller-provided identifier verbatim |
| **enabled** | **True** | Enable or disable instrumentation |
| **delivery** | **"background"** | Use **"await"** for serverless or short-lived processes |
| **timeout_ms** | **500** | Set the delivery timeout |
| **emit** | Network emitter | Replace delivery for tests or custom pipelines |
| **on_error** | None | Observe delivery failures |
| **capture_telemetry** | **True** | Disable conversation-derived telemetry entirely (see below) |
| **redact_secrets** | **True** | Disable only built-in high-confidence secret matching |
| **redact** | None | Redact sensitive data from previews before delivery (see below) |
| **redact_event** | None | Sync/async whole-event hook that may mutate or drop a tool call |
| **schedule** | None | Register background work with a serverless lifecycle primitive |
| **telemetry_field_map** | None | Export existing argument fields as telemetry (see below) |
| **request_capability** | **False** | Inject `request_capability` so agents can report an unmet tool need |

### Capability requests

Set **request_capability: True** to dynamically add a `request_capability`
tool. It accepts one required `capability` string and uses this description
exactly:

> Request a capability that is not provided by the currently available tools. Use this when a capability is required to complete the user’s request and no existing tool can perform it.

Calls are captured by the normal analytics pipeline and feed Armature's
unmet-demand signals. The tool is not added when the option is omitted or
false, when **enabled: False**, or when no API key/custom **emit** delivery is
configured. The camelCase alias **requestCapability** is also accepted.

### Telemetry capture and privacy

The SDK injects an optional `telemetry` parameter (`user_intent`, `agent_thinking`, `user_frustration`) into each wrapped tool. This is conversation-derived data: if your deployment cannot disclose it — for example in a privacy policy required for an app-store submission — set **capture_telemetry: False**. With capture off, tool schemas, signatures, and descriptions pass through completely untouched, and telemetry sent by clients holding an older cached schema is stripped and never delivered anywhere (ingest, `emit`, or `on_error`). Tool-call and session analytics keep working without the conversational fields.

Disclosure summary for privacy policies: with capture **on**, the SDK collects tool names, tool call inputs/outputs (size-capped previews), error messages, timing, a one-way hash of the actor seed, the verbatim `actor_identifier` when configured, client name/version, and the agent-supplied `telemetry` fields above; recipients are your Armature workspace. With capture **off**, the `telemetry` fields are not collected.

If a tool function already declares its own `telemetry` parameter (or an explicit schema declares the property), the SDK treats that field as **yours**: signature, schema, and arguments pass through untouched, nothing is interpreted as Armature telemetry, and a warning is logged once at registration. To export an existing, semantically equivalent field, opt in explicitly with **telemetry_field_map** — e.g. `{"user_intent": "purpose"}` reads (never strips) the tool's `purpose` argument into `user_intent`. Explicit `telemetry` values always win over mapped ones, and the map is ignored while capture is off.

### Redaction and binary payloads

Before serialization, the SDK bounds sanitizer work to 65,536 characters, removes binary/base64 payloads, and applies default-on high-confidence secret rules to inputs, outputs, errors, and telemetry text. Set **redact_secrets: False** only to disable secret matching; binary sanitization remains active.

The legacy synchronous **redact** callable runs next. Prefer sync-or-async **redact_event** for new integrations: it receives the whole prepared tool-call candidate and may mutate it or return `None` to drop the tool event. The order is bounded sanitization → built-in secret rules → `redact` → `redact_event` → stringify → truncate. Exceptions fail closed with `"[redaction failed]"` placeholders.

CamelCase aliases such as **endpointUrl**, **apiKey**, **actorId**,
**actorIdentifier**, **timeoutMs**, and **onError** are
accepted for JavaScript parity.

### Delivery

- **"background"** queues privacy work on the event loop. Use it for long-lived processes and call **await instrumentation.recorder.flush()** during shutdown.
- **"await"** drains sanitization, hooks, and delivery before returning. Use it for serverless functions and short-lived processes.

The FIFO queue batches up to 20 candidates, holds at most 1,000, and drops the oldest candidate on overflow. A platform lifecycle callable may be passed as **schedule** (for example, `context.wait_until`).

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

Optional **actor_identifier** may be a string or sync/async resolver using the
same input as **actor_id**. Its contents are not interpreted: it may be an
internal ID, email, name, or any other non-empty string. The value is sent
verbatim in an **actor_identity** event and hashed into `actor_id`. An event is
emitted only when the value changes. The only additional limit is an 8 KiB cap.
When **actor_identifier** is absent, **actor_id** retains its existing hashed-
only behavior.

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
