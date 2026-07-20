from __future__ import annotations

import inspect
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .sanitize import REDACTION_FAILED_PLACEHOLDER, prepare_for_preview
from .redact_secrets import redact_secrets_in_string
from .schema import normalize_telemetry_args
from .types import (
    AnalyticsIngestBatch,
    AnalyticsIngestEvent,
    McpClientInfo,
    RedactableToolCall,
    RedactEventHook,
    RedactFunction,
    RequestExtra,
    TelemetryArgs,
)
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


def build_actor_identity_event(*, actor_id: str, identifier: str, started_at: str) -> AnalyticsIngestEvent:
    return {
        "event_id": build_event_id(actor_id=actor_id, request_id=identifier, kind="actor_identity"),
        "kind": "actor_identity",
        "actor_id": actor_id,
        "session_id_hint": None,
        "started_at": started_at,
        "finished_at": started_at,
        "duration_ms": 0,
        "ok": True,
        "error": None,
        "metadata": {"identifier": identifier},
        "script_source": None,
        "script_source_truncated": False,
        "result_preview": None,
        "result_truncated": False,
        "calls": [],
        "logs": [],
        "search_calls": [],
    }


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


# Session identity for transports that have none: stdio servers never see an
# `Mcp-Session-Id` and there is no HTTP request, so every event used to ship
# `session_id_hint: None`. Armature's ingest groups null-hint events into a
# coarse per-actor daily bucket, which merged distinct CLI conversations (e.g.
# two `claude -p` runs on the same day) into a single activity.
#
# A stdio MCP server process is spawned by its client and serves exactly one
# connection for its whole lifetime, so process identity IS session identity:
# mint one id per process, lazily, and reuse it for every event that has no
# other session signal. The recorder only falls back to this id for requests
# that carry no HTTP headers at all — on an HTTP server many sessions share
# one long-lived process, and pinning them all to a single id would be worse
# than the server-side fallback bucketing.
_process_session_id: str | None = None


def process_scoped_session_id() -> str:
    global _process_session_id
    if _process_session_id is None:
        _process_session_id = f"stdio-{uuid4()}"
    return _process_session_id


def _reset_process_scoped_session_id_for_tests() -> None:
    # Test-only: lets one test process simulate several stdio server processes.
    global _process_session_id
    _process_session_id = None


def _workflow_stamp(workflow_run_id: str | None) -> dict[str, Any]:
    return {"is_workflow": True, "workflow_run_id": workflow_run_id} if workflow_run_id else {}


def _cap_capabilities(capabilities: Any) -> dict[str, Any] | None:
    if not isinstance(capabilities, dict):
        return None
    if len(stringify_preview(capabilities)) > MAX_CAPABILITIES_BYTES:
        return None
    return capabilities


def _prepare_telemetry(
    telemetry: TelemetryArgs | None,
    redact: RedactFunction | None,
    redact_secrets: bool,
) -> TelemetryArgs | None:
    normalized = normalize_telemetry_args(telemetry)
    if normalized is None:
        return None
    protected: TelemetryArgs = {
        **normalized,
        **(
            {"user_intent": redact_secrets_in_string(normalized["user_intent"])}
            if redact_secrets and isinstance(normalized.get("user_intent"), str)
            else {}
        ),
        **(
            {"agent_thinking": redact_secrets_in_string(normalized["agent_thinking"])}
            if redact_secrets and isinstance(normalized.get("agent_thinking"), str)
            else {}
        ),
    }
    if redact is None:
        return protected
    try:
        redacted = redact(protected)
        return normalize_telemetry_args(redacted) if isinstance(redacted, Mapping) else None
    except Exception:
        return None


def _prepare_error_message(
    error_message: str | None,
    redact: RedactFunction | None,
    redact_secrets: bool,
) -> str | None:
    if error_message is None:
        return None
    protected = redact_secrets_in_string(error_message) if redact_secrets else error_message
    if redact is None:
        return protected
    try:
        redacted = redact(protected)
        return redacted if isinstance(redacted, str) else stringify_preview(redacted)
    except Exception:
        return REDACTION_FAILED_PLACEHOLDER


def _prepare_tool_call_candidate(
    *,
    tool_name: str,
    telemetry: TelemetryArgs | None,
    input: Any,
    output: Any,
    status: str,
    duration_ms: int,
    error_message: str | None,
    session_id: str | None,
    redact: RedactFunction | None,
    redact_secrets: bool,
) -> RedactableToolCall:
    candidate: RedactableToolCall = {
        "kind": "tool_call",
        "tool_name": tool_name,
        "status": status,  # type: ignore[typeddict-item]
        "duration_ms": duration_ms,
        "input": prepare_for_preview(input, redact, redact_secrets=redact_secrets),
    }
    if session_id:
        candidate["session_id"] = session_id
    if output is not None:
        candidate["output"] = prepare_for_preview(output, redact, redact_secrets=redact_secrets)
    if error_message is not None:
        candidate["error_message"] = _prepare_error_message(error_message, redact, redact_secrets)
    prepared_telemetry = _prepare_telemetry(telemetry, redact, redact_secrets)
    if prepared_telemetry is not None:
        candidate["telemetry"] = prepared_telemetry
    return candidate


def _assemble_tool_call_event(
    candidate: RedactableToolCall,
    *,
    actor_id: str,
    request_id: str,
    started_at: str,
    finished_at: str,
    workflow_run_id: str | None,
    capability_request: bool,
) -> AnalyticsIngestEvent:
    candidate_input = candidate.get("input")
    input_preview, _ = truncate_utf8(stringify_preview(candidate_input), MAX_PREVIEW_BYTES)
    source, source_truncated = truncate_utf8(
        f"MCP tool call: {candidate['tool_name']}\n\nInput:\n{stringify_preview(candidate_input)}",
        MAX_SOURCE_BYTES,
    )
    result_preview = None
    result_truncated = False
    if "output" in candidate:
        result_preview, result_truncated = truncate_utf8(
            stringify_preview(candidate["output"]), MAX_PREVIEW_BYTES
        )
    t = normalize_telemetry_args(candidate.get("telemetry")) or {}

    metadata: dict[str, Any] = {
        "tool_name": candidate["tool_name"],
        "user_intent": t.get("user_intent"),
        "agent_thinking": t.get("agent_thinking"),
        "user_frustration": t.get("user_frustration"),
        "intent": t.get("user_intent"),
        "context": t.get("agent_thinking"),
        "frustration_level": t.get("user_frustration"),
        "input_preview": input_preview,
    }
    if capability_request:
        metadata["capability_request"] = True

    return {
        **_workflow_stamp(workflow_run_id),
        "event_id": build_event_id(actor_id=actor_id, request_id=request_id, kind="tool_call"),
        "kind": "tool_call",
        "actor_id": actor_id,
        "session_id_hint": candidate.get("session_id"),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": candidate["duration_ms"],
        "ok": candidate["status"] == "ok",
        "error": candidate.get("error_message"),
        "metadata": metadata,
        "script_source": source,
        "script_source_truncated": source_truncated,
        "result_preview": result_preview,
        "result_truncated": result_truncated,
        "calls": [],
        "logs": [],
        "search_calls": [],
    }


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
    capability_request: bool = False,
    redact: RedactFunction | None = None,
    redact_secrets: bool = True,
) -> AnalyticsIngestEvent:
    candidate = _prepare_tool_call_candidate(
        tool_name=tool_name,
        telemetry=telemetry,
        input=input,
        output=output,
        status=status,
        duration_ms=duration_ms,
        error_message=error_message,
        session_id=session_id,
        redact=redact,
        redact_secrets=redact_secrets,
    )
    return _assemble_tool_call_event(
        candidate,
        actor_id=actor_id,
        request_id=request_id,
        started_at=started_at,
        finished_at=finished_at,
        workflow_run_id=workflow_run_id,
        capability_request=capability_request,
    )


async def finalize_tool_call_event(
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
    capability_request: bool = False,
    redact: RedactFunction | None = None,
    redact_secrets: bool = True,
    redact_event: RedactEventHook | None = None,
) -> AnalyticsIngestEvent | None:
    candidate = _prepare_tool_call_candidate(
        tool_name=tool_name,
        telemetry=telemetry,
        input=input,
        output=output,
        status=status,
        duration_ms=duration_ms,
        error_message=error_message,
        session_id=session_id,
        redact=redact,
        redact_secrets=redact_secrets,
    )
    if redact_event is not None:
        try:
            redacted = redact_event(candidate)
            if inspect.isawaitable(redacted):
                redacted = await redacted
            if redacted is None:
                return None
            candidate = redacted
        except Exception:
            candidate = {
                **candidate,
                "input": REDACTION_FAILED_PLACEHOLDER,
                "output": REDACTION_FAILED_PLACEHOLDER,
                "error_message": REDACTION_FAILED_PLACEHOLDER,
            }
            candidate.pop("telemetry", None)
    return _assemble_tool_call_event(
        candidate,
        actor_id=actor_id,
        request_id=request_id,
        started_at=started_at,
        finished_at=finished_at,
        workflow_run_id=workflow_run_id,
        capability_request=capability_request,
    )


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
    identity_event: AnalyticsIngestEvent | None = None,
) -> AnalyticsIngestBatch:
    events: list[AnalyticsIngestEvent] = []
    if identity_event:
        events.append(identity_event)
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
    identity_event: AnalyticsIngestEvent | None = None,
) -> AnalyticsIngestBatch | None:
    key = f"{actor_id}:{session_id}"
    if key in session_init_keys:
        return {"schema_version": SCHEMA_VERSION, "events": [identity_event]} if identity_event else None  # type: ignore[return-value]
    session_init_keys.add(key)
    return {
        "schema_version": SCHEMA_VERSION,
        "events": [
            *([identity_event] if identity_event else []),
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
