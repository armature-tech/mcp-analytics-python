"""Armature analytics helpers for Python MCP servers."""

from .emit import post_telemetry_event
from .events import (
    build_actor_id,
    build_event_id,
    build_session_init_event,
    build_tool_call_event,
    normalize_session_id,
)
from .recorder import AnalyticsRecorder, create_analytics_recorder
from .sanitize import (
    BASE64_REMOVED_PLACEHOLDER,
    BINARY_REMOVED_PLACEHOLDER,
    REDACTION_FAILED_PLACEHOLDER,
    prepare_for_preview,
    sanitize_value,
)
from .schema import (
    ToolTelemetryPlan,
    append_telemetry_hint,
    apply_telemetry_field_map,
    create_telemetry_json_schema,
    decorate_input_schema_with_telemetry,
    extract_telemetry_arguments,
    is_capture_enabled,
    normalize_telemetry_args,
    plan_tool_telemetry,
    schema_declares_telemetry,
)
from .server import FastMCPInstrumentation, instrument_fastmcp, with_mcp_analytics
from .stateless_http import (
    StatelessHttpSession,
    StatelessHttpSessionMiddleware,
    build_stateless_session_id,
    parse_stateless_session_client_info,
    resolve_stateless_http_session,
)
from .types import (
    AnalyticsConfig,
    AnalyticsIngestBatch,
    AnalyticsIngestEvent,
    McpClientInfo,
    RedactFunction,
    TelemetryArgs,
    TelemetryFieldMap,
    TelemetryMode,
)

__all__ = [
    "AnalyticsConfig",
    "AnalyticsIngestBatch",
    "AnalyticsIngestEvent",
    "AnalyticsRecorder",
    "BASE64_REMOVED_PLACEHOLDER",
    "BINARY_REMOVED_PLACEHOLDER",
    "FastMCPInstrumentation",
    "McpClientInfo",
    "REDACTION_FAILED_PLACEHOLDER",
    "RedactFunction",
    "StatelessHttpSession",
    "StatelessHttpSessionMiddleware",
    "TelemetryArgs",
    "TelemetryFieldMap",
    "TelemetryMode",
    "ToolTelemetryPlan",
    "append_telemetry_hint",
    "apply_telemetry_field_map",
    "build_actor_id",
    "build_event_id",
    "build_session_init_event",
    "build_stateless_session_id",
    "build_tool_call_event",
    "create_analytics_recorder",
    "create_telemetry_json_schema",
    "decorate_input_schema_with_telemetry",
    "extract_telemetry_arguments",
    "instrument_fastmcp",
    "is_capture_enabled",
    "normalize_session_id",
    "normalize_telemetry_args",
    "plan_tool_telemetry",
    "post_telemetry_event",
    "parse_stateless_session_client_info",
    "prepare_for_preview",
    "resolve_stateless_http_session",
    "sanitize_value",
    "schema_declares_telemetry",
    "with_mcp_analytics",
]
