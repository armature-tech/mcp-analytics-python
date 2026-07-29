# Changelog

## Unreleased

### Client attribution for stateful (handshake-era) servers

Stateful servers process the `initialize` handshake inside their transport,
so the adapter never observed `clientInfo` and sessions surfaced with
Client "Unknown" — telemetry and session ids were perfect, identity was
null. This affected standalone fastmcp 2.x/3.x (HTTP transport, stateful)
and official-SDK `mcp.server.fastmcp` servers with `stateless_http=False`
(both verified live).

- **Handshake identity is now recovered at tool-call time** from the
  transport session: the SDK's `ServerSession` retains the `initialize`
  params as `session.client_params` (`clientInfo` name/version,
  `protocolVersion`, `capabilities`). The adapter reads it duck-typed and
  exception-safe from the injected/captured request context's `session`, or
  — for v1 ambient surfaces (standalone fastmcp 2/3 and
  `mcp.server.fastmcp`, both built on the v1 lowlevel server) — from the
  ambient `request_ctx` ContextVar. Stateless-era per-request `_meta`
  identity stays authoritative when both exist.
- **`session_init` now carries the identity** on the first recorded call of
  each session (same process-local per-session dedup as before: exactly one
  `session_init` per session id), so ingest coalesces
  `client_name`/`client_version`/`protocol_version`/`capabilities` onto the
  session row.
- **Tool_call events are stamped with `client_name`/`client_version`/
  `protocol_version` too** when known (TypeScript v2-adapter parity),
  feeding the ingest work-event coalesce. Absent identity keeps today's
  event shape — no null-filled keys, no fabricated `session_init`.
- Covers stdio servers as well: the retained handshake now attributes the
  process-scoped stdio session's client.

### MCP SDK v2 / spec 2026-07-28 support

The MCP 2026-07-28 revision is stateless: no `initialize` handshake, no
`Mcp-Session-Id`. Client identity travels per-request in `params._meta`
under reserved `io.modelcontextprotocol/*` keys, and trace context
(`traceparent`, `tracestate`, `baggage`) rides the same `_meta` dict. The
official Python SDK ships this era as `mcp` 2.x (same package name) and
renames `mcp.server.fastmcp.FastMCP` to `mcp.server.mcpserver.MCPServer`;
fastmcp 4 is the matching standalone line.

- **`instrument_fastmcp` now instruments `mcp.server.mcpserver.MCPServer`**
  (SDK v2) with the same decorator-wrap approach used for FastMCP. SDK v2
  removed the ambient `request_ctx` ContextVar, so the adapter appends a
  keyword-only, schema-invisible `Context` parameter to each wrapped tool and
  reads headers via `ctx.headers` / `ctx.request_context.request.headers` and
  the raw `_meta` via `ctx.request_context.meta`. Works with the dual-era
  `MCPServer.streamable_http_app()` (one endpoint serving both the legacy
  handshake and the modern per-request envelope).
- **fastmcp 4 support**: the tool-decorator instrumentation binds unchanged on
  `fastmcp>=4.0.0a2`; per-request `_meta` and headers are read from fastmcp 4's
  ambient `fastmcp_request_ctx` (a fastmcp-owned ContextVar wrapping the SDK
  v2 injected context). fastmcp 2.x/3.x paths are unchanged.
- **Stateless-era session identity ladder** (highest priority first):
  `gen_ai.conversation.id` from the `baggage` `_meta` key (W3C baggage,
  URL-decoded) → `x-armature-session-seed` header → legacy `Mcp-Session-Id`
  header when a handshake-era client sent one → process-scoped stdio id only
  when there is genuinely no HTTP request → none (server-side bucketing).
  `session_init` is emitted on first sight of a session key, with
  `client_name` / `client_version` / `protocol_version` / `capabilities`
  taken from the per-request `_meta` envelope (`clientInfo` is optional;
  its absence yields an unknown client, never a crash).
- **`request_meta` capture**: the raw request `_meta` dict is recorded
  verbatim into tool_call event metadata as `request_meta`, capped at 4 KB
  with an explicit truncation marker and `request_meta_truncated` flag.
  `AnalyticsRecorder.dispatch` context accepts `requestMeta`/`request_meta`.
- **Loud v1-degradation guard**: when `mcp>=2` is installed and a tool call
  reaches the recorder with no injected or ambient request context (the
  situation where the removed `request_ctx` used to be silently swallowed and
  every session merged into one process-scoped bucket), the adapter now emits
  a one-time `warnings.warn(RuntimeWarning)` + `logger.warning` explaining
  that session attribution will degrade. An instrument-time variant fires for
  unrecognized server objects under `mcp>=2`. The host server never crashes.
- **`StatelessHttpSessionMiddleware` is handshake-era only**: it now detects
  modern-era requests (a `_meta` protocol-version envelope or an
  `MCP-Protocol-Version: 2026-07-28+` header), logs a one-time warning, and
  passes them through untouched instead of minting a fabricated one-request
  session id per POST. Legacy behavior is unchanged.
- **Pins widened**: extras are now `mcp>=1.27,<3` and `fastmcp>=2,<5`, and are
  co-installable with fastmcp 4 + mcp 2. Note: fastmcp `4.0.0a2` itself
  hard-pins `mcp==2.0.0b2`, so it cannot share an environment with mcp
  `2.0.0rc1`; CI covers the two combinations separately.
- **Tests**: new `tests/test_sdk_v2.py` (ladder priority, `_meta` envelope
  parsing, request-meta capping, guard warnings, MCPServer end-to-end over
  the dual-era HTTP app, fastmcp 4 client + ambient-context paths) plus a
  modern-era no-op test for the stateless middleware. CI gained
  `official-mcp2` and `fastmcp4` legs gated by `ARMATURE_REQUIRE_MCP2` /
  `ARMATURE_REQUIRE_FASTMCP4`.

The wire contract (`packages/TELEMETRY-CONTRACT.md`) is unchanged.
