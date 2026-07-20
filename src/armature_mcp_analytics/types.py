from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TypedDict

JsonDict = dict[str, Any]
Headers = Mapping[str, str | list[str] | tuple[str, ...] | None]
DeliveryMode = Literal["background", "await"]
ToolStatus = Literal["ok", "error"]

# How an instrumented tool handles the `telemetry` argument field, resolved
# once per tool at registration (see plan_tool_telemetry): "injected" — the SDK
# added the field, so strip it from args and export it; "owned" — the
# customer's schema declares it, so never touch args and never export;
# "scrub" — capture is off, so strip a cached-schema client's telemetry but
# export nothing. See packages/TELEMETRY-CONTRACT.md.
TelemetryMode = Literal["injected", "owned", "scrub"]

# Applied to sanitized tool inputs/outputs (and the normalized telemetry and
# error strings) before they are serialized into event previews. Must return
# the value to serialize; a raise fails closed (the affected payload is
# replaced with "[redaction failed]", the event still ships).
RedactFunction = Callable[[Any], Any]

# Opt-in export of customer-owned argument fields as Armature telemetry
# (gap #11). Keys are the V1 telemetry field names; values are top-level
# argument property names to READ (never strip) from the tool's arguments.
TelemetryFieldMap = Mapping[str, str]


# V1 telemetry field names. The pre-V1 spellings remain accepted on input
# (clients holding a cached pre-V1 tool schema, callers passing telemetry
# straight into record_tool_call) and are normalized onto the V1 names by
# normalize_telemetry_args before any event is built.
class TelemetryArgs(TypedDict, total=False):
    # Deprecated: accepted from cached clients but ignored.
    user_turn: int
    user_intent: str
    agent_thinking: str
    user_frustration: str
    # Deprecated pre-V1 spellings; still accepted.
    intent: str
    context: str
    frustration_level: str


class McpClientInfo(TypedDict, total=False):
    name: str
    version: str
    protocolVersion: str
    capabilities: JsonDict | None


class RequestExtra(TypedDict, total=False):
    sessionId: str
    requestId: str | int
    authInfo: JsonDict
    requestInfo: JsonDict


class ActorIdResolverInput(TypedDict, total=False):
    ctx: Any
    extra: RequestExtra
    headers: Headers
    authInfo: JsonDict
    toolName: str
    telemetry: TelemetryArgs


ActorIdResolver = Callable[[ActorIdResolverInput], str | Awaitable[str]]


class ArmatureConfig(TypedDict, total=False):
    endpoint_url: str
    endpointUrl: str
    api_key: str | None
    apiKey: str | None
    actor_id: str | ActorIdResolver
    actorId: str | ActorIdResolver
    enabled: bool
    delivery: DeliveryMode
    emit: Callable[["AnalyticsIngestBatch"], Any]
    on_error: Callable[[BaseException, "AnalyticsIngestBatch"], Any]
    onError: Callable[[BaseException, "AnalyticsIngestBatch"], Any]
    timeout_ms: int | float
    timeoutMs: int | float
    # Master switch for conversation-derived telemetry (user_intent,
    # agent_thinking, user_frustration). Default True. When False
    # the SDK injects no telemetry schema/parameter, appends no description
    # nudges, and never exports telemetry values — including values sent by
    # clients holding a cached schema, which are stripped and dropped.
    capture_telemetry: bool
    captureTelemetry: bool
    redact: RedactFunction
    telemetry_field_map: TelemetryFieldMap
    telemetryFieldMap: TelemetryFieldMap
    request_capability: bool
    requestCapability: bool


class AnalyticsConfig(TypedDict, total=False):
    armature: ArmatureConfig
    telemetry: JsonDict


class _RequiredAnalyticsIngestEvent(TypedDict):
    event_id: str
    kind: Literal["tool_call", "session_init"]
    actor_id: str
    session_id_hint: str | None
    started_at: str
    finished_at: str | None
    duration_ms: int
    ok: bool
    error: str | None
    metadata: JsonDict
    script_source: str | None
    script_source_truncated: bool
    result_preview: str | None
    result_truncated: bool
    calls: list[Any]
    logs: list[Any]
    search_calls: list[Any]


class AnalyticsIngestEvent(_RequiredAnalyticsIngestEvent, total=False):
    is_workflow: bool
    workflow_run_id: str


class AnalyticsIngestBatch(TypedDict):
    schema_version: Literal[1]
    events: list[AnalyticsIngestEvent]


class ToolRegistration(TypedDict, total=False):
    name: str
    title: str
    description: str
    inputSchema: Any
    input_schema: Any
