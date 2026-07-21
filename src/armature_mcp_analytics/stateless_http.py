"""Session identity helpers for stateless HTTP and serverless MCPs.

The identity-bearing session ID format intentionally matches the TypeScript
and Go SDKs. MCP clients echo the server-issued ``Mcp-Session-Id`` on later
requests, allowing each cold invocation to recover the client name/version
without a shared session store.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .types import Headers, JsonDict, McpClientInfo

_SESSION_ID_RE = re.compile(
    r"^mcp_([A-Za-z0-9.-]+)_v_([A-Za-z0-9.-]*)_"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_SESSION_SEED_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_ANONYMOUS_NAME = "unknown"


def _slug_part(value: Any, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", str(value or "").strip())
    slug = slug.strip("-")[:48]
    return slug or fallback


def build_stateless_session_id(
    client_info: McpClientInfo | None = None,
    session_seed: str | None = None,
) -> str:
    """Mint a session ID carrying the initialize request's client identity."""

    info = client_info or {}
    name = _slug_part(info.get("name"), _ANONYMOUS_NAME)
    version = _slug_part(info.get("version"), "")
    seed = str(session_seed or "").strip()
    session_uuid = seed.lower() if _SESSION_SEED_RE.fullmatch(seed) else str(uuid4())
    return f"mcp_{name}_v_{version}_{session_uuid}"


def parse_stateless_session_client_info(session_id: str | None) -> McpClientInfo | None:
    """Recover best-effort client identity from an Armature stateless ID."""

    match = _SESSION_ID_RE.fullmatch(str(session_id or ""))
    if not match or match.group(1) == _ANONYMOUS_NAME:
        return None
    info: McpClientInfo = {"name": match.group(1)}
    if match.group(2):
        info["version"] = match.group(2)
    return info


def _header_value(headers: Headers | None, name: str) -> str | None:
    if not headers:
        return None
    lower = name.lower()
    for key, raw in headers.items():
        if key.lower() != lower:
            continue
        if isinstance(raw, (list, tuple)):
            return str(raw[0]) if raw else None
        return str(raw) if raw is not None else None
    return None


def _find_initialize(body: Any) -> JsonDict | None:
    messages = body if isinstance(body, list) else [body]
    for message in messages:
        if isinstance(message, dict) and message.get("method") == "initialize":
            return message
    return None


def _client_info_from_initialize(message: JsonDict | None) -> McpClientInfo | None:
    if not message:
        return None
    params = message.get("params")
    info = params.get("clientInfo") if isinstance(params, dict) else None
    if not isinstance(info, dict):
        return None
    result: McpClientInfo = {}
    if isinstance(info.get("name"), str):
        result["name"] = info["name"]
    if isinstance(info.get("version"), str):
        result["version"] = info["version"]
    return result


def _has_success_result(payload: Any, request_id: Any) -> bool:
    messages = payload if isinstance(payload, list) else [payload]
    return any(
        isinstance(message, dict)
        and message.get("id") == request_id
        and "result" in message
        and "error" not in message
        for message in messages
    )


def _is_successful_initialize_response(raw: bytes, request_id: Any) -> bool:
    """Accept JSON or SSE JSON-RPC responses only when they contain a result."""

    try:
        return _has_success_result(json.loads(raw), request_id)
    except (TypeError, ValueError):
        pass
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            if _has_success_result(json.loads(line[5:].strip()), request_id):
                return True
        except (TypeError, ValueError):
            continue
    return False


@dataclass(frozen=True)
class StatelessHttpSession:
    """Resolved identity for one stateless MCP HTTP request."""

    session_id: str
    is_initialize: bool
    client_info: McpClientInfo | None = None

    @property
    def session_id_generator(self) -> Callable[[], str] | None:
        """Transport generator for initialize; ``None`` on later requests."""

        if not self.is_initialize:
            return None
        return lambda: self.session_id

    @property
    def dispatch_context(self) -> JsonDict:
        """Context fields accepted by ``AnalyticsRecorder.dispatch``."""

        context: JsonDict = {"sessionId": self.session_id}
        if self.client_info:
            context["clientInfo"] = self.client_info
        return context


def resolve_stateless_http_session(
    *,
    body: Any = None,
    headers: Headers | None = None,
) -> StatelessHttpSession:
    """Resolve a stable stateless session from an MCP body and HTTP headers."""

    initialize = _find_initialize(body)
    if initialize is not None:
        session_id = build_stateless_session_id(
            _client_info_from_initialize(initialize),
            _header_value(headers, "x-armature-session-seed"),
        )
        return StatelessHttpSession(session_id=session_id, is_initialize=True)

    echoed = (_header_value(headers, "mcp-session-id") or "").strip()
    session_id = echoed or str(uuid4())
    return StatelessHttpSession(
        session_id=session_id,
        is_initialize=False,
        client_info=parse_stateless_session_client_info(session_id),
    )


class StatelessHttpSessionMiddleware:
    """ASGI middleware providing stable sessions to stateless FastMCP apps.

    FastMCP and the official Python SDK do not currently expose a session-ID
    generator hook. This middleware supplies the same contract at the HTTP
    boundary: initialize responses issue the identity-bearing ID, and later
    requests retain (or receive a one-off fallback for) ``Mcp-Session-Id``.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: JsonDict,
        receive: Callable[..., Any],
        send: Callable[..., Any],
    ) -> None:
        if (
            scope.get("type") != "http"
            or str(scope.get("method", "")).upper() != "POST"
        ):
            await self.app(scope, receive, send)
            return

        messages: list[JsonDict] = []
        raw = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            raw.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break

        try:
            body = json.loads(raw or b"null")
        except (TypeError, ValueError):
            body = None

        headers_list = list(scope.get("headers") or [])
        decoded_headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in headers_list
        }
        session = (
            resolve_stateless_http_session(body=body, headers=decoded_headers)
            if body is not None
            else None
        )
        initialize = _find_initialize(body)
        initialize_request_id = initialize.get("id") if initialize else None

        request_scope = dict(scope)
        if (
            session is not None
            and not session.is_initialize
            and not _header_value(decoded_headers, "mcp-session-id")
        ):
            headers_list.append((b"mcp-session-id", session.session_id.encode("ascii")))
            request_scope["headers"] = headers_list

        index = 0

        async def replay_receive() -> JsonDict:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            # Past the recorded body, defer to the live channel. Returning a
            # synthetic http.disconnect here kills SSE responses: the official
            # SDK's streamable-http transport watches receive() for client
            # disconnect while streaming, and an instant disconnect aborts the
            # response before anything is written.
            return await receive()

        pending_start: JsonDict | None = None
        pending_body: list[JsonDict] = []

        async def session_send(message: JsonDict) -> None:
            nonlocal pending_start
            if session is None or not session.is_initialize:
                await send(message)
                return

            if message.get("type") == "http.response.start":
                if 200 <= int(message.get("status", 0)) < 300:
                    pending_start = message
                    return
                await send(message)
                return

            if (
                message.get("type") == "http.response.body"
                and pending_start is not None
            ):
                pending_body.append(message)
                if message.get("more_body", False):
                    return
                raw_response = b"".join(part.get("body", b"") for part in pending_body)
                response_headers = [
                    (key, value)
                    for key, value in pending_start.get("headers", [])
                    if key.lower() != b"mcp-session-id"
                ]
                if _is_successful_initialize_response(
                    raw_response, initialize_request_id
                ):
                    response_headers.append(
                        (b"mcp-session-id", session.session_id.encode("ascii"))
                    )
                await send({**pending_start, "headers": response_headers})
                for part in pending_body:
                    await send(part)
                pending_start = None
                pending_body.clear()
                return

            # Flush a pending start before an unexpected ASGI message so the
            # middleware never leaves the response protocol half-written.
            if pending_start is not None:
                await send(pending_start)
                pending_start = None
                pending_body.clear()
            await send(message)

        await self.app(request_scope, replay_receive, session_send)
