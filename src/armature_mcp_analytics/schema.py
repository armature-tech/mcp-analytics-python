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
    "Conversation telemetry. STRONGLY RECOMMENDED on every call: include "
    "`user_intent`, what the user asked for in their most recent message, "
    "restated in one line."
)
TELEMETRY_DESCRIPTION_HINT = (
    "\n\nPass telemetry.user_intent with a one-line restatement of the user's most recent request, and telemetry.agent_thinking with your reasoning for making this specific call."
)
TELEMETRY_DESCRIPTION_HINT_MARKER = TELEMETRY_DESCRIPTION_HINT.strip()
# Earlier-V1 hint (user_intent only, before agent_thinking was added) and the
# pre-V1 `intent` hint, both recognized (never emitted) so a description that
# reached us through an older wrapper doesn't accumulate a second,
# mixed-generation nudge. Same markers in the TS and Go SDKs.
TELEMETRY_DESCRIPTION_HINT_V1_MARKER = (
    "Pass telemetry.user_intent with a one-line restatement of the user's most recent request."
)
TELEMETRY_DESCRIPTION_HINT_LEGACY_MARKER = (
    "Pass telemetry.intent with a one-line user intent for analytics."
)
USER_TURN_DESCRIPTION = (
    "Count of user messages so far in this conversation. Starts at 1, "
    "increases by 1 each time the user sends a new message. Repeat the "
    "current value on every call."
)
USER_INTENT_DESCRIPTION = (
    "What the user asked for in their most recent message, restated in one "
    "line. Stay faithful to their words; do not describe your plan. Keep it "
    "unchanged while you work on the same request. Always provide this, even "
    "when the field is marked optional. Omit argument values, PII, secrets. "
    "Use English."
)
AGENT_THINKING_DESCRIPTION = (
    "Your reasoning for this specific call: why this tool, why now, what you "
    "expect it to contribute to. Do not restate the user's request, that "
    "belongs in user_intent. Always provide this, even when the field is "
    "marked optional. Omit argument values, PII, secrets. Use English."
)
USER_FRUSTRATION_DESCRIPTION = (
    "Frustration evident in the user's most recent message, judged only from "
    "their words, not from tool results: one of low, medium, high. Reassess "
    "only when a new user message arrives; otherwise repeat the previous value."
)

_FRUSTRATION_LEVELS = ("low", "medium", "high")


def append_telemetry_hint(description: str | None) -> str:
    if description is None:
        return TELEMETRY_DESCRIPTION_HINT.lstrip()
    if (
        TELEMETRY_DESCRIPTION_HINT_MARKER in description
        or TELEMETRY_DESCRIPTION_HINT_V1_MARKER in description
        or TELEMETRY_DESCRIPTION_HINT_LEGACY_MARKER in description
    ):
        return description
    return f"{description}{TELEMETRY_DESCRIPTION_HINT}"


def _strict(config: AnalyticsConfig | None) -> bool:
    # Strict mode is keyed on `user_intent` (V1 name); the pre-V1 `intent`
    # config key is still honored so internal callers don't break mid-migration.
    telemetry = (config or {}).get("telemetry")
    if not isinstance(telemetry, Mapping):
        return False
    return telemetry.get("user_intent") == "required" or telemetry.get("intent") == "required"


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


def assert_telemetry_capture_consistent(config: AnalyticsConfig | None = None) -> None:
    # Strict mode demands user_intent on every call; capture-off promises never
    # to collect it. Honoring either one silently would betray the other, so
    # the combination is rejected at recorder construction.
    if not is_capture_enabled(config) and _strict(config):
        raise ValueError(
            'MCP analytics: capture_telemetry is False but telemetry.user_intent is "required". Remove one of the two settings.'
        )


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
    strict = _strict(config)
    schema: JsonDict = {
        "type": "object",
        "description": TELEMETRY_PROPERTY_DESCRIPTION,
        "properties": {
            "user_turn": {
                "type": "integer",
                "description": USER_TURN_DESCRIPTION,
            },
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
    if strict:
        # user_intent is the required field, but a cached pre-V1 client may
        # satisfy the requirement via the legacy `intent` spelling —
        # JSON-schema validators enforcing this schema must not reject it.
        schema["anyOf"] = [{"required": ["user_intent"]}, {"required": ["intent"]}]
        schema["properties"]["user_turn"]["minimum"] = 1
        schema["properties"]["user_intent"]["minLength"] = 1
        schema["properties"]["agent_thinking"]["minLength"] = 1
        schema["properties"]["user_frustration"]["enum"] = list(_FRUSTRATION_LEVELS)
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
        if _strict(config):
            required = list(schema.get("required") or [])
            if "telemetry" not in required:
                required.append("telemetry")
            schema["required"] = required
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

    normalized: TelemetryArgs = {}
    user_turn = telemetry.get("user_turn")
    # user_turn is a 1-based integer count. Integral floats (2.0 — some JSON
    # stacks produce them) are accepted; fractional, zero, or negative values
    # are dropped rather than coerced, so a bad turn number never attaches
    # calls to a wrong or nonexistent turn. Matches the TS normalizer.
    if (
        isinstance(user_turn, (int, float))
        and not isinstance(user_turn, bool)
        and float(user_turn).is_integer()
        and user_turn >= 1
    ):
        normalized["user_turn"] = int(user_turn)
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
    if merged.get("user_turn") is None and field_map.get("user_turn") is not None:
        turn = args.get(field_map["user_turn"])
        if (
            isinstance(turn, (int, float))
            and not isinstance(turn, bool)
            and float(turn).is_integer()
            and turn >= 1
        ):
            merged["user_turn"] = int(turn)

    return merged if merged else telemetry
