"""Support for MCP SDK v2 (spec revision 2026-07-28) and fastmcp 4.

The 2026-07-28 protocol revision is stateless: there is no ``initialize``
handshake and no ``Mcp-Session-Id``. Client identity travels per-request in
``params._meta`` under reserved ``io.modelcontextprotocol/*`` keys, and trace
context (``traceparent`` / ``tracestate`` / ``baggage``) rides the same
``_meta`` dict as plain keys.

The Python SDK v2 (``mcp>=2``) removed the ambient
``mcp.server.lowlevel.server.request_ctx`` ContextVar that the v1 adapter used
to reach HTTP headers; context is now *injected* into handlers. This module
holds everything the adapter needs to work in that world:

- version detection (``installed_mcp_major``) and the loud degradation warning
  for code paths that predate SDK v2 (``warn_mcp2_context_gap``);
- duck-typed capture of the injected ``mcp.server.mcpserver.Context`` (and of
  fastmcp's ``Context`` / ambient ``fastmcp_request_ctx`` on fastmcp>=4);
- the ``_meta`` envelope readers (clientInfo / protocolVersion / capabilities,
  with graceful fallback to literal reverse-DNS keys when ``mcp-types`` is not
  importable);
- the session-identity ladder for the stateless era.

Session-identity ladder (highest priority first):

1. ``gen_ai.conversation.id`` from the ``baggage`` ``_meta`` key
   (W3C baggage: comma-separated ``key=value`` pairs, URL-decoded);
2. the ``x-armature-session-seed`` HTTP header;
3. the legacy ``Mcp-Session-Id`` HTTP header when a handshake-era client sent
   one (the dual-era ``streamable_http_app`` serves both eras);
4. the process-scoped stdio id — only when there is genuinely no HTTP request
   (delegated to the recorder via header presence);
5. ``None`` (server-side bucketing at ingest).
"""

from __future__ import annotations

import logging
import re
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from .types import McpClientInfo
from .utils import header_value

logger = logging.getLogger("armature_mcp_analytics")

# Reserved request `_meta` keys (spec revision 2026-07-28). Sourced from the
# companion `mcp-types` package when it is installed; the literal reverse-DNS
# strings are the wire contract, so falling back to them keeps the adapter
# working without a hard dependency on mcp-types.
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
try:  # pragma: no cover - exercised only when mcp-types is installed
    from mcp_types import (  # type: ignore[import-not-found]
        CLIENT_CAPABILITIES_META_KEY as _MCP_TYPES_CAPS_KEY,
        CLIENT_INFO_META_KEY as _MCP_TYPES_CLIENT_INFO_KEY,
        PROTOCOL_VERSION_META_KEY as _MCP_TYPES_PROTOCOL_KEY,
    )

    CLIENT_INFO_META_KEY = _MCP_TYPES_CLIENT_INFO_KEY
    PROTOCOL_VERSION_META_KEY = _MCP_TYPES_PROTOCOL_KEY
    CLIENT_CAPABILITIES_META_KEY = _MCP_TYPES_CAPS_KEY
except Exception:
    pass

GEN_AI_CONVERSATION_ID_BAGGAGE_KEY = "gen_ai.conversation.id"
SESSION_SEED_HEADER = "x-armature-session-seed"

# Name of the keyword-only Context parameter the adapter appends to wrapper
# signatures registered on an official-SDK v2 MCPServer, so the tool manager
# injects the per-request context into the wrapper even when the customer
# function declares no Context parameter of its own. Skipped from the
# advertised schema by the SDK's own context_kwarg detection.
ARMATURE_CTX_KWARG = "armature_analytics_ctx"

_MCP_MAJOR_UNSET = object()
_installed_mcp_major: Any = _MCP_MAJOR_UNSET


def installed_mcp_major() -> int | None:
    """Major version of the installed ``mcp`` distribution, or None."""
    global _installed_mcp_major
    if _installed_mcp_major is _MCP_MAJOR_UNSET:
        try:
            from importlib.metadata import version

            match = re.match(r"(\d+)", version("mcp"))
            _installed_mcp_major = int(match.group(1)) if match else None
        except Exception:
            _installed_mcp_major = None
    return _installed_mcp_major


_warned_once: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned_once:
        return
    _warned_once.add(key)
    # Both channels on purpose: warnings.warn reaches test harnesses and
    # `python -W error` deployments, logger.warning reaches production logs.
    warnings.warn(message, RuntimeWarning, stacklevel=4)
    logger.warning(message)


def _reset_warnings_for_tests() -> None:
    _warned_once.clear()


def warn_mcp2_context_gap() -> None:
    """LOUD guard for the v1 ambient-context path running under mcp>=2.

    Under SDK v1 the adapter read HTTP headers from the ambient
    ``request_ctx`` ContextVar. SDK v2 removed it, and the old behavior was to
    swallow that import error silently — reintroducing the all-sessions-merged
    bug (every request falls through to the process-scoped stdio session id or
    to per-actor daily bucketing). Warn instead; never crash the host server.
    """
    _warn_once(
        "mcp2-context-gap",
        "armature-mcp-analytics: MCP Python SDK v2 (mcp>=2) is installed, but no "
        "per-request context reached this tool call. SDK v2 removed the ambient "
        "request_ctx ContextVar that the v1 integration path relied on, so "
        "session attribution will degrade: concurrent conversations may merge "
        "into one process-scoped session or lose session identity entirely. "
        "Instrument a supported server object (mcp.server.mcpserver.MCPServer "
        "or fastmcp>=4 FastMCP) so the adapter can capture the injected "
        "request context, or pin mcp<2 until you migrate.",
    )


def warn_mcp2_unknown_server(server: Any) -> None:
    """Instrument-time guard: unrecognized server object under mcp>=2."""
    _warn_once(
        "mcp2-unknown-server",
        "armature-mcp-analytics: instrumenting %r while MCP Python SDK v2 "
        "(mcp>=2) is installed. This server object is not a recognized SDK v2 "
        "surface (mcp.server.mcpserver.MCPServer or fastmcp FastMCP), and the "
        "SDK v2 removed the ambient request context the v1 adapter relied on "
        "— session attribution will degrade unless per-request context is "
        "passed explicitly." % type(server).__name__,
    )


@dataclass
class RequestContextCapture:
    """Normalized view of one request's transport context."""

    headers: Any = None  # mapping when the transport carried HTTP headers
    meta: dict[str, Any] | None = None  # raw request `_meta`, verbatim
    has_http_request: bool = False
    protocol_version: str | None = None


def _meta_as_dict(meta: Any) -> dict[str, Any] | None:
    if isinstance(meta, Mapping):
        return dict(meta)
    if meta is None:
        return None
    # SDK v1 RequestParams.Meta is a pydantic model with extra keys allowed.
    dump = getattr(meta, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump(by_alias=True, exclude_none=True)
            return dict(dumped) if isinstance(dumped, Mapping) else None
        except Exception:
            return None
    return None


def capture_from_context_object(candidate: Any) -> RequestContextCapture | None:
    """Duck-typed capture from an injected Context-like object.

    Works for ``mcp.server.mcpserver.Context`` (SDK v2), fastmcp's ``Context``
    (whose ``request_context`` is a ``FastMCPRequestContext``), and the SDK
    v1 FastMCP ``Context``. Returns None when ``candidate`` is not a context
    object; returns an *empty* capture (stdio-shaped) when it is one but has
    no active request — in-process calls must keep the stdio fallback, not
    trip the degradation warning.
    """
    if candidate is None or isinstance(candidate, (Mapping, str, bytes, int, float, bool, list, tuple)):
        return None
    if not hasattr(type(candidate), "request_context"):
        return None
    try:
        request_context = candidate.request_context
    except Exception:
        # SDK v2 Context raises ValueError outside a request.
        request_context = None
    if request_context is None:
        return RequestContextCapture()
    return capture_from_request_context(request_context)


def capture_from_request_context(request_context: Any) -> RequestContextCapture:
    """Capture from a ``ServerRequestContext`` / ``FastMCPRequestContext``."""
    meta = _meta_as_dict(getattr(request_context, "meta", None))
    if meta is None:
        # fastmcp<4 and the SDK v1 keep the raw `_meta` block on the params
        # mapping; `meta` there is a progress-token-only model.
        params = getattr(request_context, "params", None)
        if isinstance(params, Mapping):
            meta = _meta_as_dict(params.get("_meta"))
    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None)
    protocol_version = getattr(request_context, "protocol_version", None)
    return RequestContextCapture(
        headers=headers,
        meta=meta,
        has_http_request=request is not None,
        protocol_version=protocol_version if isinstance(protocol_version, str) else None,
    )


# Import-once cache for fastmcp>=4's ambient request ContextVar. fastmcp 4
# rebuilt the per-request surface on top of SDK v2's injected context and owns
# its own ContextVar (`fastmcp_request_ctx`), bound around every handler —
# including stdio — carrying the lifted raw `_meta` dict and the HTTP request.
_FASTMCP4_CTX_UNSET = object()
_fastmcp4_request_ctx: Any = _FASTMCP4_CTX_UNSET


def _load_fastmcp4_request_ctx() -> Any:
    global _fastmcp4_request_ctx
    if _fastmcp4_request_ctx is _FASTMCP4_CTX_UNSET:
        try:
            from fastmcp.server.dependencies import fastmcp_request_ctx

            _fastmcp4_request_ctx = fastmcp_request_ctx
        except Exception:
            _fastmcp4_request_ctx = None
    return _fastmcp4_request_ctx


def capture_from_fastmcp_ambient() -> RequestContextCapture | None:
    ctx_var = _load_fastmcp4_request_ctx()
    if ctx_var is None:
        return None
    try:
        wrapper = ctx_var.get()
    except Exception:
        return None
    if wrapper is None:
        return None
    return capture_from_request_context(wrapper)


def parse_baggage(raw: str) -> dict[str, str]:
    """Parse a W3C ``baggage`` value: comma-separated ``key=value`` members.

    Values are URL-decoded; per-member properties (``;``-suffixed) are
    dropped. Malformed members are skipped rather than failing the request.
    """
    entries: dict[str, str] = {}
    for member in raw.split(","):
        key, sep, value = member.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.split(";", 1)[0].strip()
        if not key:
            continue
        try:
            entries[key] = unquote(value)
        except Exception:
            entries[key] = value
    return entries


def resolve_session_key(meta: Mapping[str, Any] | None, headers: Any) -> str | None:
    """Ladder steps 1-3 (see module docstring); steps 4-5 are the recorder's
    header-presence fallback."""
    if isinstance(meta, Mapping):
        baggage = meta.get("baggage")
        if isinstance(baggage, str) and baggage:
            conversation_id = parse_baggage(baggage).get(GEN_AI_CONVERSATION_ID_BAGGAGE_KEY)
            if isinstance(conversation_id, str) and conversation_id.strip():
                return conversation_id.strip()
    for header in (SESSION_SEED_HEADER, "mcp-session-id"):
        value = header_value(headers, header)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def client_info_from_meta(
    meta: Mapping[str, Any] | None,
    protocol_version: str | None = None,
) -> McpClientInfo | None:
    """Client identity for session_init from the per-request ``_meta`` envelope.

    clientInfo is OPTIONAL on the wire (the required per-request pair is
    protocolVersion + clientCapabilities): absent clientInfo yields an
    entry without name/version — an unknown client, never a crash.
    """
    if not isinstance(meta, Mapping):
        return None
    result: McpClientInfo = {}
    info = meta.get(CLIENT_INFO_META_KEY)
    if isinstance(info, Mapping):
        if isinstance(info.get("name"), str) and info["name"].strip():
            result["name"] = info["name"]
        if isinstance(info.get("version"), str) and info["version"].strip():
            result["version"] = info["version"]
    envelope_version = meta.get(PROTOCOL_VERSION_META_KEY)
    effective_version = envelope_version if isinstance(envelope_version, str) else protocol_version
    if isinstance(effective_version, str) and effective_version.strip():
        result["protocolVersion"] = effective_version
    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    if isinstance(capabilities, Mapping):
        result["capabilities"] = dict(capabilities)
    return result or None


def apply_capture_to_context(context: dict[str, Any], capture: RequestContextCapture) -> None:
    """Fold a capture into a `_context_from_call`-style context dict."""
    if capture.has_http_request:
        headers = capture.headers
        try:
            normalized = dict(headers) if headers is not None else {}
        except Exception:
            normalized = {}
        # `headers` present (even empty) disarms the recorder's stdio
        # fallback; absent keeps it armed — exactly ladder steps 4-5.
        context.setdefault("headers", normalized)
    session_key = resolve_session_key(capture.meta, capture.headers)
    if session_key:
        context.setdefault("session_id", session_key)
    client_info = client_info_from_meta(capture.meta, capture.protocol_version)
    if client_info:
        context.setdefault("client_info", client_info)
    if capture.meta:
        context.setdefault("request_meta", dict(capture.meta))
