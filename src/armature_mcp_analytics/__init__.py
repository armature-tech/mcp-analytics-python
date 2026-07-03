"""Armature analytics helpers for Python MCP servers."""

from .events import (
    build_actor_id,
    build_event_id,
    build_session_init_event,
    build_tool_call_event,
    normalize_session_id,
)
from .emit import post_telemetry_event
from .recorder import AnalyticsRecorder, create_analytics_recorder
from .schema import (
    append_telemetry_hint,
    create_telemetry_json_schema,
    decorate_input_schema_with_telemetry,
    extract_telemetry_arguments,
)
from .server import FastMCPInstrumentation, instrument_fastmcp, with_mcp_analytics
from .types import (
    AnalyticsConfig,
    AnalyticsIngestBatch,
    AnalyticsIngestEvent,
    McpClientInfo,
    TelemetryArgs,
)

__all__ = [
    "AnalyticsConfig",
    "AnalyticsIngestBatch",
    "AnalyticsIngestEvent",
    "AnalyticsRecorder",
    "FastMCPInstrumentation",
    "McpClientInfo",
    "TelemetryArgs",
    "append_telemetry_hint",
    "build_actor_id",
    "build_event_id",
    "build_session_init_event",
    "build_tool_call_event",
    "create_analytics_recorder",
    "create_telemetry_json_schema",
    "decorate_input_schema_with_telemetry",
    "extract_telemetry_arguments",
    "instrument_fastmcp",
    "normalize_session_id",
    "post_telemetry_event",
    "with_mcp_analytics",
]
