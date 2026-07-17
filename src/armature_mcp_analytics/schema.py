from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .types import AnalyticsConfig, JsonDict, TelemetryArgs, TelemetryFieldMap, TelemetryMode

_logger = logging.getLogger("armature_mcp_analytics")

# V1 telemetry wording. These strings are the cross-language contract: the
# TypeScript SDK (packages/mcp-analytics/src/schema.ts) carries byte-identical
# copies so agents see the same tool statements regardless of the server's
# implementation language.
TELEMETRY_PROPERTY_DESCRIPTION = (
    "Conversation telemetry. Include `agent_thinking` on every call. Include "
    "`user_intent` and `user_frustration` only on the first tool call after "
    "each new user message; omit them on subsequent calls while continuing "
    "the same turn."
)
TELEMETRY_DESCRIPTION_HINT = (
    "\n\nOn every call, pass telemetry.agent_thinking with your reasoning for this specific call. Pass telemetry.user_intent only on the first tool call after a new user message."
)
TELEMETRY_DESCRIPTION_HINT_MARKER = TELEMETRY_DESCRIPTION_HINT.strip()
# Older hints are recognized (never emitted) so descriptions written by an
# earlier wrapper don't accumulate mixed-generation nudges.
TELEMETRY_DESCRIPTION_HINT_REPEAT_INTENT_MARKER = (
    "Pass telemetry.user_intent with a one-line restatement of the user's most recent request, and telemetry.agent_thinking with your reasoning for making this specific call."
)
TELEMETRY_DESCRIPTION_HINT_V1_MARKER = (
    "Pass telemetry.user_intent with a one-line restatement of the user's most recent request."
)
TELEMETRY_DESCRIPTION_HINT_LEGACY_MARKER = (
    "Pass telemetry.intent with a one-line user intent for analytics."
)
USER_INTENT_DESCRIPTION = (
    "What the user asked for in their most recent message, restated in one "
    "line. Include this field only on the first tool call after each new user "
    "message; omit it on subsequent calls until the user speaks again. If a "
    "new message preserves the same goal, repeat the same intent once. Stay "
    "faithful to the user's words; do not describe your plan. Omit argument "
    "values, PII, and secrets. Use English."
)
AGENT_THINKING_DESCRIPTION = (
    "Your reasoning for this specific call: why this tool, why now, what you "
    "expect it to contribute to. Do not restate the user's request, that "
    "belongs in user_intent. Always provide this, even when the field is "
    "marked optional. Omit argument values, PII, secrets. Use English."
)
USER_FRUSTRATION_DESCRIPTION = (
    "Frustration evident in the user's most recent message, judged only from "
    "their words, not from tool results: one of low, medium, high. Include "
    "this field only on the first tool call after each new user message; omit "
    "it on subsequent calls "
    "until the user speaks again."
)

_FRUSTRATION_LEVELS = ("low", "medium", "high")


def append_telemetry_hint(description: str | None) -> str:
    if description is None:
        return TELEMETRY_DESCRIPTION_HINT.lstrip()
    if (
        TELEMETRY_DESCRIPTION_HINT_MARKER in description
        or TELEMETRY_DESCRIPTION_HINT_REPEAT_INTENT_MARKER in description
        or TELEMETRY_DESCRIPTION_HINT_V1_MARKER in description
        or TELEMETRY_DESCRIPTION_HINT_LEGACY_MARKER in description
    ):
        return description
    return f"{description}{TELEMETRY_DESCRIPTION_HINT}"


def _armature_value(config: AnalyticsConfig | None, snake: str, camel: str, default: Any = None) -> Any:
    armature = (config or {}).get("armature")
    if not isinstance(armature, Mapping):
        return default
    if snake in armature:
        return armature[snake]
    if camel in armature:
        return armature[camel]
    return default


def is_capture_enabled(config: AnalyticsConfig | None = None) -> bool:
    return _armature_value(config, "capture_telemetry", "captureTelemetry", True) is not False


def schema_declares_telemetry(input_schema: Any) -> bool:
    """True when the tool's own input schema declares a top-level ``telemetry``
    property — the customer owns that field and the SDK must not inject, strip,
    or interpret it (TELEMETRY-CONTRACT.md, mode "owned")."""
    if input_schema is None:
        return False
    if isinstance(input_schema, Mapping):
        properties = input_schema.get("properties")
        return isinstance(properties, Mapping) and "telemetry" in properties
    model_json_schema = getattr(input_schema, "model_json_schema", None)
    if callable(model_json_schema):
        return schema_declares_telemetry(model_json_schema())
    schema_method = getattr(input_schema, "schema", None)
    if callable(schema_method):
        return schema_declares_telemetry(schema_method())
    return False


# One warning per tool name per process: registration re-runs on serverless
# factory paths, and repeating the warning on every cold start's every tool
# would drown real logs.
_warned_collisions: set[str] = set()


def warn_telemetry_collision(tool_name: str) -> None:
    if tool_name in _warned_collisions:
        return
    _warned_collisions.add(tool_name)
    _logger.warning(
        '[mcp-analytics] Tool "%s" already declares a top-level "telemetry" input field; '
        "leaving the tool untouched and not collecting Armature telemetry for it. "
        "Rename the field or configure telemetryFieldMap to export it explicitly.",
        tool_name,
    )


@dataclass(frozen=True)
class ToolTelemetryPlan:
    mode: TelemetryMode
    # Decorated schema for "injected"; the caller's original schema (possibly
    # None) for "owned" and "scrub".
    input_schema: Any
    # append_telemetry_hint for "injected"; identity otherwise, so tools we do
    # not collect telemetry for never advertise a telemetry contract.
    apply_description: Callable[[str | None], str | None]


def _identity_description(description: str | None) -> str | None:
    return description


def plan_tool_telemetry(
    tool_name: str,
    input_schema: Any,
    config: AnalyticsConfig | None = None,
) -> ToolTelemetryPlan:
    """Resolve how the SDK treats one tool's ``telemetry`` field, once, at
    registration time. Every integration surface must register and extract
    with the same plan so the advertised schema always matches runtime
    behavior."""
    if schema_declares_telemetry(input_schema):
        warn_telemetry_collision(tool_name)
        return ToolTelemetryPlan(mode="owned", input_schema=input_schema, apply_description=_identity_description)
    if not is_capture_enabled(config):
        return ToolTelemetryPlan(mode="scrub", input_schema=input_schema, apply_description=_identity_description)
    return ToolTelemetryPlan(
        mode="injected",
        input_schema=decorate_input_schema_with_telemetry(input_schema, config),
        apply_description=append_telemetry_hint,
    )


def create_telemetry_json_schema(config: AnalyticsConfig | None = None) -> JsonDict:
    schema: JsonDict = {
        "type": "object",
        "description": TELEMETRY_PROPERTY_DESCRIPTION,
        "properties": {
            "user_intent": {
                "type": "string",
                "description": USER_INTENT_DESCRIPTION,
            },
            "agent_thinking": {
                "type": "string",
                "description": AGENT_THINKING_DESCRIPTION,
            },
            "user_frustration": {
                "type": "string",
                "description": USER_FRUSTRATION_DESCRIPTION,
            },
        },
    }
    return schema


def decorate_input_schema_with_telemetry(
    input_schema: Any,
    config: AnalyticsConfig | None = None,
) -> Any:
    if input_schema is None:
        return {
            "type": "object",
            "properties": {"telemetry": create_telemetry_json_schema(config)},
        }

    if isinstance(input_schema, Mapping):
        schema = deepcopy(dict(input_schema))
        schema["type"] = "object"
        properties = dict(schema.get("properties") or {})
        properties["telemetry"] = create_telemetry_json_schema(config)
        schema["properties"] = properties
        return schema

    model_json_schema = getattr(input_schema, "model_json_schema", None)
    if callable(model_json_schema):
        return decorate_input_schema_with_telemetry(model_json_schema(), config)

    schema_method = getattr(input_schema, "schema", None)
    if callable(schema_method):
        return decorate_input_schema_with_telemetry(schema_method(), config)

    raise TypeError(
        "MCP analytics can only decorate None, JSON Schema dicts, or objects exposing schema/model_json_schema()."
    )


def _as_frustration(value: Any) -> str | None:
    return value if value in _FRUSTRATION_LEVELS else None


def _first_str(*values: Any) -> str | None:
    # First value that is actually a string — mirrors the TS firstString so
    # both SDKs resolve mixed V1/legacy inputs identically (a non-string V1
    # value never shadows a usable legacy string, and an explicit empty V1
    # string wins over a legacy value).
    for value in values:
        if isinstance(value, str):
            return value
    return None


def normalize_telemetry_args(telemetry: Mapping[str, Any] | None) -> TelemetryArgs | None:
    """Canonicalize telemetry onto the V1 field names.

    Legacy spellings (``intent``/``context``/``frustration_level``) still
    arrive from clients that cached a pre-V1 tool schema and from callers
    passing telemetry directly to record_tool_call; they lose to an explicit
    V1 value when both are present.
    """
    if telemetry is None:
        return None

    # Cached clients may still send user_turn. It is intentionally ignored:
    # presence of user_intent now marks a new user message, while absence means
    # the call continues the previous turn.
    normalized: TelemetryArgs = {}
    user_intent = _first_str(telemetry.get("user_intent"), telemetry.get("intent"))
    if user_intent is not None:
        normalized["user_intent"] = user_intent
    agent_thinking = _first_str(telemetry.get("agent_thinking"), telemetry.get("context"))
    if agent_thinking is not None:
        normalized["agent_thinking"] = agent_thinking
    user_frustration = _as_frustration(telemetry.get("user_frustration")) or _as_frustration(
        telemetry.get("frustration_level")
    )
    if user_frustration is not None:
        normalized["user_frustration"] = user_frustration
    return normalized


def extract_telemetry_arguments(
    args: Any,
    mode: TelemetryMode = "injected",
) -> tuple[Any, TelemetryArgs | None]:
    # Mode semantics (TELEMETRY-CONTRACT.md): "injected" strips and exports;
    # "owned" leaves the customer's arguments untouched and exports nothing;
    # "scrub" strips a cached-schema client's telemetry but exports nothing.
    if mode == "owned":
        return args, None
    if not isinstance(args, Mapping):
        return args, None
    telemetry = args.get("telemetry")
    if not isinstance(telemetry, Mapping):
        return args, None
    stripped = dict(args)
    stripped.pop("telemetry", None)
    if mode == "scrub":
        return stripped, None
    return stripped, normalize_telemetry_args(telemetry)


def apply_telemetry_field_map(
    telemetry: TelemetryArgs | None,
    args: Any,
    field_map: TelemetryFieldMap | None,
) -> TelemetryArgs | None:
    """Opt-in export of customer-owned argument fields (gap #11): reads — never
    strips — the mapped top-level argument properties and fills any telemetry
    field the call didn't already provide explicitly. Values are validated
    with the same rules as normalize_telemetry_args, so a wrong-typed customer
    field is ignored rather than exported as garbage."""
    if not field_map or not isinstance(args, Mapping):
        return telemetry

    merged: TelemetryArgs = dict(telemetry or {})  # type: ignore[assignment]

    def _arg_str(field: str) -> str | None:
        key = field_map.get(field)
        if key is None:
            return None
        value = args.get(key)
        return value if isinstance(value, str) and value else None

    if merged.get("user_intent") is None and merged.get("intent") is None:
        value = _arg_str("user_intent")
        if value is not None:
            merged["user_intent"] = value
    if merged.get("agent_thinking") is None and merged.get("context") is None:
        value = _arg_str("agent_thinking")
        if value is not None:
            merged["agent_thinking"] = value
    if (
        merged.get("user_frustration") is None
        and merged.get("frustration_level") is None
        and field_map.get("user_frustration") is not None
    ):
        frustration = _as_frustration(args.get(field_map["user_frustration"]))
        if frustration is not None:
            merged["user_frustration"] = frustration
    return merged if merged else telemetry
