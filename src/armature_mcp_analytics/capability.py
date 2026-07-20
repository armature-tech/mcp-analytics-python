from __future__ import annotations

from typing import Any

from .emit import _config_value, resolve_api_key
from .types import AnalyticsConfig, ToolRegistration


REQUEST_CAPABILITY_TOOL_NAME = "request_capability"
REQUEST_CAPABILITY_DESCRIPTION = (
    "Request a capability that is not provided by the currently available tools. "
    "Use this when a capability is required to complete the user’s request and no "
    "existing tool can perform it."
)
REQUEST_CAPABILITY_ACKNOWLEDGMENT = "Capability request acknowledged."
REQUEST_CAPABILITY_ARGUMENT_DESCRIPTION = (
    "The capability required to complete the user's request. Omit argument "
    "values, PII, and secrets. Use English."
)


def request_capability_enabled(config: AnalyticsConfig | None) -> bool:
    armature = (config or {}).get("armature") or {}
    if armature.get("enabled") is False:
        return False
    requested = (
        armature.get("request_capability") is True
        or armature.get("requestCapability") is True
    )
    has_delivery = callable(_config_value(config, "emit", "emit")) or bool(
        resolve_api_key(config)
    )
    return requested and has_delivery


def request_capability_registration() -> ToolRegistration:
    return {
        "name": REQUEST_CAPABILITY_TOOL_NAME,
        "description": REQUEST_CAPABILITY_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "description": REQUEST_CAPABILITY_ARGUMENT_DESCRIPTION,
                    "minLength": 1,
                    "maxLength": 1000,
                },
            },
            "required": ["capability"],
            "additionalProperties": False,
        },
    }


def acknowledge_capability_request(args: Any = None, _context: Any = None) -> str:
    capability = args.get("capability") if isinstance(args, dict) else None
    if (
        not isinstance(capability, str)
        or not capability.strip()
        or len(capability) > 1000
    ):
        raise ValueError("capability must be a non-empty string of at most 1000 characters")
    return REQUEST_CAPABILITY_ACKNOWLEDGMENT
