from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .types import AnalyticsIngestBatch, AnalyticsIngestEvent, McpClientInfo, RequestExtra, TelemetryArgs
from .utils import (
    BoundedKeySet,
    MAX_CAPABILITIES_BYTES,
    MAX_PREVIEW_BYTES,
    MAX_SOURCE_BYTES,
    SCHEMA_VERSION,
    header_value,
    sha256_hex,
    stringify_preview,
    truncate_utf8,
)


def _trim_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_started_at(
    started_at: str | datetime | int | float | None = None,
    *,
    duration_ms: int | None = None,
    finished_at_ms: float | None = None,
) -> str:
    if isinstance(started_at, datetime):
        dt = started_at.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    if isinstance(started_at, str):
        return started_at
    if isinstance(started_at, (int, float)) and started_at > 1_000_000_000_000:
        return datetime.fromtimestamp(started_at / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    if duration_ms is not None and finished_at_ms is not None:
        return datetime.fromtimestamp((finished_at_ms - duration_ms) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    return _iso_now()


def build_actor_id(*, actor_seed: str) -> str:
    return sha256_hex(actor_seed)


def build_event_id(*, actor_id: str, request_id: str, kind: str) -> str:
    return sha256_hex(f"{actor_id} {kind} {request_id}")


def normalize_session_id(
    event_session_id: str | None = None,
    extra: RequestExtra | None = None,
) -> str | None:
    explicit = _trim_or_none(event_session_id) or _trim_or_none((extra or {}).get("sessionId"))
    if explicit:
        return explicit
    request_info = (extra or {}).get("requestInfo")
    headers = request_info.get("headers") if isinstance(request_info, dict) else None
    return _trim_or_none(header_value(headers, "mcp-session-id"))


def normalize_request_id(event_request_id: str | None = None) -> str:
    return event_request_id or str(uuid4())


def _workflow_stamp(workflow_run_id: str | None) -> dict[str, Any]:
    return {"is_workflow": True, "workflow_run_id": workflow_run_id} if workflow_run_id else {}


def _cap_capabilities(capabilities: Any) -> dict[str, Any] | None:
    if not isinstance(capabilities, dict):
        return None
    if len(stringify_preview(capabilities)) > MAX_CAPABILITIES_BYTES:
        return None
    return capabilities


def build_tool_call_event(
    *,
    tool_name: str,
    telemetry: TelemetryArgs | None,
    input: Any,
    output: Any = None,
    status: str,
    duration_ms: int,
    error_message: str | None,
    actor_id: str,
    session_id: str | None,
    request_id: str,
    started_at: str,
    finished_at: str,
    workflow_run_id: str | None = None,
) -> AnalyticsIngestEvent:
    input_preview, _ = truncate_utf8(stringify_preview(input), MAX_PREVIEW_BYTES)
    source, source_truncated = truncate_utf8(
        f"MCP tool call: {tool_name}\n\nInput:\n{stringify_preview(input)}",
        MAX_SOURCE_BYTES,
    )
    result_preview = None
    result_truncated = False
    if output is not None:
        result_preview, result_truncated = truncate_utf8(stringify_preview(output), MAX_PREVIEW_BYTES)

    return {
        **_workflow_stamp(workflow_run_id),
        "event_id": build_event_id(actor_id=actor_id, request_id=request_id, kind="tool_call"),
        "kind": "tool_call",
        "actor_id": actor_id,
        "session_id_hint": session_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "ok": status == "ok",
        "error": error_message,
        "metadata": {
            "tool_name": tool_name,
            "intent": (telemetry or {}).get("intent"),
            "context": (telemetry or {}).get("context"),
            "frustration_level": (telemetry or {}).get("frustration_level"),
            "input_preview": input_preview,
        },
        "script_source": source,
        "script_source_truncated": source_truncated,
        "result_preview": result_preview,
        "result_truncated": result_truncated,
        "calls": [],
        "logs": [],
        "search_calls": [],
    }


def build_session_init_event(
    *,
    actor_id: str,
    session_id: str,
    started_at: str,
    extra: RequestExtra | None = None,
    client_info: McpClientInfo | None = None,
    workflow_run_id: str | None = None,
) -> AnalyticsIngestEvent:
    request_info = (extra or {}).get("requestInfo")
    headers = request_info.get("headers") if isinstance(request_info, dict) else None
    auth_info = (extra or {}).get("authInfo") or {}
    return {
        **_workflow_stamp(workflow_run_id),
        "event_id": build_event_id(actor_id=actor_id, request_id=session_id, kind="session_init"),
        "kind": "session_init",
        "actor_id": actor_id,
        "session_id_hint": session_id,
        "started_at": started_at,
        "finished_at": started_at,
        "duration_ms": 0,
        "ok": True,
        "error": None,
        "metadata": {
            "client_name": _trim_or_none((client_info or {}).get("name"))
            or _trim_or_none(auth_info.get("clientId"))
            or _trim_or_none(header_value(headers, "x-mcp-client")),
            "client_version": _trim_or_none((client_info or {}).get("version")),
            "protocol_version": _trim_or_none((client_info or {}).get("protocolVersion")),
            "capabilities": _cap_capabilities((client_info or {}).get("capabilities")),
            "user_agent": header_value(headers, "user-agent"),
        },
        "script_source": None,
        "script_source_truncated": False,
        "result_preview": None,
        "result_truncated": False,
        "calls": [],
        "logs": [],
        "search_calls": [],
    }


def build_batch(
    *,
    event: AnalyticsIngestEvent,
    extra: RequestExtra | None,
    actor_id: str,
    started_at: str,
    session_init_keys: BoundedKeySet,
    client_info: McpClientInfo | None = None,
    workflow_run_id: str | None = None,
) -> AnalyticsIngestBatch:
    events: list[AnalyticsIngestEvent] = []
    session_id = (extra or {}).get("sessionId")
    if session_id:
        key = f"{actor_id}:{session_id}"
        if key not in session_init_keys:
            session_init_keys.add(key)
            events.append(
                build_session_init_event(
                    actor_id=actor_id,
                    session_id=session_id,
                    started_at=started_at,
                    extra=extra,
                    client_info=client_info,
                    workflow_run_id=workflow_run_id,
                )
            )
    events.append(event)
    return {"schema_version": SCHEMA_VERSION, "events": events}  # type: ignore[return-value]


def build_session_init_batch(
    *,
    actor_id: str,
    session_id: str,
    started_at: str,
    extra: RequestExtra | None,
    session_init_keys: BoundedKeySet,
    client_info: McpClientInfo | None = None,
    workflow_run_id: str | None = None,
) -> AnalyticsIngestBatch | None:
    key = f"{actor_id}:{session_id}"
    if key in session_init_keys:
        return None
    session_init_keys.add(key)
    return {
        "schema_version": SCHEMA_VERSION,
        "events": [
            build_session_init_event(
                actor_id=actor_id,
                session_id=session_id,
                started_at=started_at,
                extra=extra,
                client_info=client_info,
                workflow_run_id=workflow_run_id,
            )
        ],
    }  # type: ignore[return-value]
