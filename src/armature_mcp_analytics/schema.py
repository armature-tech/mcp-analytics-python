from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .capability import request_capability_enabled
from .types import (
    AnalyticsConfig,
    DescriptionLengthLogLevel,
    JsonDict,
    TelemetryArgs,
    TelemetryFieldMap,
    TelemetryMode,
)

_logger = logging.getLogger("armature_mcp_analytics")

# Agent-facing wording is shared across all four SDKs. Storage keeps the
# existing agent_thinking/context fields for compatibility.
TELEMETRY_PROPERTY_DESCRIPTION = 'Optional task context for usage analytics, based on the visible user request and the action performed by this tool.'
TELEMETRY_DESCRIPTION_HINT = '\n\nInclude telemetry.call_purpose with a short description of this action. Include telemetry.user_intent and telemetry.user_frustration only on the first tool call after each new user message.'
TELEMETRY_HINT_TELEMETRY_SENTENCE = 'Include telemetry.call_purpose with a short description of this action. Include telemetry.user_intent and telemetry.user_frustration only on the first tool call after each new user message.'
TELEMETRY_HINT_REQUEST_CAPABILITY_SENTENCE = 'Call request_capability before you tell the user something can\'t be done here or has to be done elsewhere.'
TELEMETRY_DESCRIPTION_HINT_WITH_REQUEST_CAPABILITY = '\n\nInclude telemetry.call_purpose with a short description of this action. Include telemetry.user_intent and telemetry.user_frustration only on the first tool call after each new user message. Call request_capability before you tell the user something can\'t be done here or has to be done elsewhere.'
TELEMETRY_DESCRIPTION_HINT_TELEMETRY_ONLY = TELEMETRY_DESCRIPTION_HINT
USER_INTENT_DESCRIPTION = "Generalized one-sentence summary of the task stated in the user's latest message. Describe actions and generic roles only. Replace all tool argument values with generic terms, including names, contacts, IDs, credentials, document titles, team names and filters. For example, 'List employees in the selected team.' Include only on the first tool call after each new user message; omit on later calls in the same turn. Use English."
CALL_PURPOSE_DESCRIPTION = "Short public description of the action this tool performs toward the user's stated goal. Base it only on the visible request, the tool's function and its inputs. Use English. Omit names, contact details, identifiers, credentials and argument values. Generalize document titles, team names and filter values (for example, 'the selected team')."
USER_FRUSTRATION_DESCRIPTION = "Frustration expressed in the user's latest message: low when none is expressed, medium for explicit dissatisfaction, high for strong or repeated dissatisfaction. Use only the user's words. Include on the first tool call after each new user message; omit on later calls in the same turn."

# Kept as an import alias for applications using the previous constant.
AGENT_THINKING_DESCRIPTION = CALL_PURPOSE_DESCRIPTION

_PREVIOUS_HINT_MARKERS = (
    'On every call, pass telemetry.agent_thinking with your reasoning for this specific call. Pass telemetry.user_intent only on the first tool call after a new user message.',
    'Pass telemetry.agent_thinking on every call, telemetry.user_intent on the first call after each user message. If no tool can do what the user asks, call request_capability.',
    'Pass telemetry.agent_thinking on every call, telemetry.user_intent on the first call after each user message.',
    "Pass telemetry.user_intent with a one-line restatement of the user's most recent request, and telemetry.agent_thinking with your reasoning for making this specific call.",
    "Pass telemetry.user_intent with a one-line restatement of the user's most recent request.",
    'Pass telemetry.intent with a one-line user intent for analytics.',
    # The current telemetry sentence with the earlier request_capability one.
    TELEMETRY_HINT_TELEMETRY_SENTENCE + ' If no tool can do what the user asks, call request_capability.',
)

# Preserve module-level constants used by existing integrations.
TELEMETRY_DESCRIPTION_HINT_MARKER = TELEMETRY_DESCRIPTION_HINT.strip()
TELEMETRY_DESCRIPTION_HINT_WITH_REQUEST_CAPABILITY_MARKER = (
    TELEMETRY_DESCRIPTION_HINT_WITH_REQUEST_CAPABILITY.strip()
)
TELEMETRY_DESCRIPTION_HINT_REPEAT_INTENT_MARKER = _PREVIOUS_HINT_MARKERS[3]
TELEMETRY_DESCRIPTION_HINT_V1_MARKER = _PREVIOUS_HINT_MARKERS[4]
TELEMETRY_DESCRIPTION_HINT_LEGACY_MARKER = _PREVIOUS_HINT_MARKERS[5]

_FRUSTRATION_LEVELS = ("low", "medium", "high")


_RECOGNIZED_HINT_MARKERS = (
    TELEMETRY_DESCRIPTION_HINT_MARKER,
    TELEMETRY_DESCRIPTION_HINT_WITH_REQUEST_CAPABILITY_MARKER,
    TELEMETRY_HINT_TELEMETRY_SENTENCE,
)


# Some clients reject a whole tools/list or request when any tool description
# exceeds this. Measured in UTF-8 bytes: conservative, and identical across the
# TS, Go, PHP and Python SDKs.
MAX_TOOL_DESCRIPTION_LENGTH = 1024


def _utf8_length(value: str) -> int:
    return len(value.encode("utf-8"))


def append_telemetry_hint(
    description: str | None,
    *,
    request_capability: bool = False,
    tool_name: str | None = None,
    log_level: DescriptionLengthLogLevel | str = "warning",
) -> str:
    """Append the per-tool telemetry hint.

    ``request_capability=True`` (the SDK-owned request_capability tool is
    enabled) selects the hint that also points agents to that tool. An
    exact older SDK suffix is replaced. Customer prose is preserved.
    The result never exceeds MAX_TOOL_DESCRIPTION_LENGTH UTF-8 bytes: when the
    full hint does not fit, omit the request_capability sentence, and when
    that does not fit either the description is left unchanged (warned once per
    ``tool_name`` when given). Nothing is ever cut inside a sentence, and the
    telemetry schema is injected either way.
    """
    hint = (
        TELEMETRY_DESCRIPTION_HINT_WITH_REQUEST_CAPABILITY
        if request_capability
        else TELEMETRY_DESCRIPTION_HINT
    )
    if description is None:
        return hint.lstrip()
    # Only whole SDK suffixes are ours to replace. A quoted or embedded hint
    # belongs to the customer's description and is preserved.
    while True:
        original = description
        for marker in _PREVIOUS_HINT_MARKERS:
            if description == marker:
                description = ""
                break
            suffix = "\n\n" + marker
            if description.endswith(suffix):
                description = description[:-len(suffix)]
                break
        if description == original:
            break
    if any(marker in description for marker in _RECOGNIZED_HINT_MARKERS):
        return description
    if request_capability and TELEMETRY_HINT_REQUEST_CAPABILITY_SENTENCE in description:
        # The customer already points agents at request_capability.
        hint = TELEMETRY_DESCRIPTION_HINT_TELEMETRY_ONLY
    hinted = f"{description}{hint}"
    if _utf8_length(hinted) <= MAX_TOOL_DESCRIPTION_LENGTH:
        return hinted
    partial = f"{description}{TELEMETRY_DESCRIPTION_HINT_TELEMETRY_ONLY}"
    if _utf8_length(partial) <= MAX_TOOL_DESCRIPTION_LENGTH:
        if tool_name is not None:
            _warn_once_per_tool(
                _warned_long_descriptions,
                tool_name,
                '[mcp-analytics] Tool "%s" description is too long for the full '
                "Armature telemetry hint within 1024 characters; appended only the "
                "telemetry sentence.",
                log_level,
            )
        return partial
    if tool_name is not None:
        _warn_once_per_tool(
            _warned_long_descriptions,
            tool_name,
            '[mcp-analytics] Tool "%s" description is too long to append the Armature '
            "telemetry hint without exceeding 1024 characters; leaving it unchanged. "
            "Telemetry is still collected.",
            log_level,
        )
    return description


def telemetry_hint_appender(
    config: AnalyticsConfig | None = None,
    tool_name: str | None = None,
) -> Callable[[str | None], str]:
    """append_telemetry_hint bound to ``config`` and ``tool_name``: the
    request_capability hint exactly when request_capability_enabled(config),
    the predicate that also decides whether the SDK injects that tool."""
    advertise_request_capability = request_capability_enabled(config)
    log_level = description_length_log_level(config)

    def apply(description: str | None) -> str:
        return append_telemetry_hint(
            description,
            request_capability=advertise_request_capability,
            tool_name=tool_name,
            log_level=log_level,
        )

    return apply


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
# Shared by both description-length warnings: at most one per tool name.
_warned_long_descriptions: set[str] = set()


_LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}


def description_length_log_level(config: AnalyticsConfig | None) -> str:
    """The configured level of the description-length notice (either alias),
    "warning" when unset."""
    armature = (config or {}).get("armature") or {}
    level = armature.get("description_length_log_level") or armature.get("descriptionLengthLogLevel")
    return level if isinstance(level, str) else "warning"


def _warn_once_per_tool(seen: set[str], tool_name: str, message: str, level: str = "warning") -> None:
    if level == "none" or tool_name in seen:
        return
    seen.add(tool_name)
    _logger.log(_LOG_LEVELS.get(level, logging.WARNING), message, tool_name)


def warn_telemetry_collision(tool_name: str) -> None:
    _warn_once_per_tool(
        _warned_collisions,
        tool_name,
        '[mcp-analytics] Tool "%s" already declares a top-level "telemetry" input field; '
        "leaving the tool untouched and not collecting Armature telemetry for it. "
        "Rename the field or configure telemetryFieldMap to export it explicitly.",
    )


@dataclass(frozen=True)
class ToolTelemetryPlan:
    mode: TelemetryMode
    # Decorated schema for "injected"; the caller's original schema (possibly
    # None) for "owned" and "scrub".
    input_schema: Any
    # telemetry_hint_appender(config, tool_name) for "injected"; identity otherwise, so tools we do
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
        apply_description=telemetry_hint_appender(config, tool_name),
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
            "call_purpose": {
                "type": "string",
                "description": CALL_PURPOSE_DESCRIPTION,
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
    """Canonicalize telemetry onto the existing ingest field names.

    ``call_purpose`` takes precedence over ``agent_thinking`` and ``context``.
    An explicit empty string is preserved.

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
    agent_thinking = _first_str(
        telemetry.get("call_purpose"), telemetry.get("agent_thinking"), telemetry.get("context")
    )
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
    if all(merged.get(field) is None for field in ("call_purpose", "agent_thinking", "context")):
        value = _arg_str("call_purpose")
        if value is None:
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
