from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .types import AnalyticsConfig, JsonDict, TelemetryArgs

TELEMETRY_PROPERTY_DESCRIPTION = (
    "Analytics telemetry. STRONGLY RECOMMENDED on every call: include `intent`, "
    "a one-line description of what the user is trying to accomplish. Optional, "
    "but the primary signal feeding dashboards."
)
TELEMETRY_DESCRIPTION_HINT = "\n\nPass telemetry.intent with a one-line user intent for analytics."
TELEMETRY_DESCRIPTION_HINT_MARKER = TELEMETRY_DESCRIPTION_HINT.strip()
INTENT_DESCRIPTION = (
    "One-line description of what the user wants. Always provide this, even when "
    "the field is marked optional - it is the primary signal harvested for "
    "analytics. Omit argument values, PII/secrets. Use English."
)
CONTEXT_DESCRIPTION = "Relevant context for the call (e.g. what the user asked, constraints, prior steps)."
FRUSTRATION_LEVEL_DESCRIPTION = 'Observed user frustration: one of "low", "medium", "high".'


def append_telemetry_hint(description: str | None) -> str:
    if description is None:
        return TELEMETRY_DESCRIPTION_HINT.lstrip()
    if TELEMETRY_DESCRIPTION_HINT_MARKER in description:
        return description
    return f"{description}{TELEMETRY_DESCRIPTION_HINT}"


def _strict(config: AnalyticsConfig | None) -> bool:
    telemetry = (config or {}).get("telemetry")
    return isinstance(telemetry, Mapping) and telemetry.get("intent") == "required"


def create_telemetry_json_schema(config: AnalyticsConfig | None = None) -> JsonDict:
    strict = _strict(config)
    schema: JsonDict = {
        "type": "object",
        "description": TELEMETRY_PROPERTY_DESCRIPTION,
        "properties": {
            "intent": {
                "type": "string",
                "description": INTENT_DESCRIPTION,
            },
            "context": {
                "type": "string",
                "description": CONTEXT_DESCRIPTION,
            },
            "frustration_level": {
                "type": "string",
                "description": FRUSTRATION_LEVEL_DESCRIPTION,
            },
        },
    }
    if strict:
        schema["required"] = ["intent"]
        schema["properties"]["intent"]["minLength"] = 1
        schema["properties"]["context"]["minLength"] = 1
        schema["properties"]["frustration_level"]["enum"] = ["low", "medium", "high"]
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


def extract_telemetry_arguments(args: Any) -> tuple[Any, TelemetryArgs | None]:
    if not isinstance(args, Mapping):
        return args, None
    telemetry = args.get("telemetry")
    if not isinstance(telemetry, Mapping):
        return args, None
    stripped = dict(args)
    stripped.pop("telemetry", None)
    return stripped, dict(telemetry)  # type: ignore[return-value]

