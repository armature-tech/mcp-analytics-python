from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TypedDict


JsonDict = dict[str, Any]
Headers = Mapping[str, str | list[str] | tuple[str, ...] | None]
DeliveryMode = Literal["background", "await"]
ToolStatus = Literal["ok", "error"]


class TelemetryArgs(TypedDict, total=False):
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


class AnalyticsConfig(TypedDict, total=False):
    armature: ArmatureConfig
    telemetry: JsonDict


class AnalyticsIngestEvent(TypedDict):
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


class AnalyticsIngestBatch(TypedDict):
    schema_version: Literal[1]
    events: list[AnalyticsIngestEvent]


class ToolRegistration(TypedDict, total=False):
    name: str
    title: str
    description: str
    inputSchema: Any
    input_schema: Any
