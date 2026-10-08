from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .emit import _config_value, resolve_api_key
from .types import AnalyticsConfig, ToolRegistration


# The SDK-owned feedback tool (TELEMETRY-CONTRACT.md, "send_feedback"). Earlier
# releases named it request_capability; only the name and the annotation title
# changed.
SEND_FEEDBACK_TOOL_NAME = "send_feedback"
SEND_FEEDBACK_DESCRIPTION = (
    "Call this before you tell the user that these tools can't do what they asked. "
    "It records the request so the developers of this server can add it. It changes "
    "no data and contacts no one. Then answer the user as usual."
)
SEND_FEEDBACK_ACKNOWLEDGMENT = "Capability request acknowledged."
# Directories such as ChatGPT's reject tools without explicit readOnlyHint,
# destructiveHint and openWorldHint. The tool records an analytics event (not
# read-only), changes no user data and reaches no one outside the server.
# Matches the hosted Armature MCP (lib/mcp/index.js).
SEND_FEEDBACK_ANNOTATIONS: dict[str, Any] = {
    "title": "Send feedback",
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
SEND_FEEDBACK_ARGUMENT_DESCRIPTION = "One English sentence describing the missing capability needed for the user's task. Translate the summary into English even when the user writes in another language. Describe generic actions and roles. Omit names, contacts, IDs, credentials and all tool argument values."

# Deprecated aliases from when the tool was named request_capability. They hold
# the new values so existing imports keep working.
REQUEST_CAPABILITY_TOOL_NAME = SEND_FEEDBACK_TOOL_NAME
REQUEST_CAPABILITY_DESCRIPTION = SEND_FEEDBACK_DESCRIPTION
REQUEST_CAPABILITY_ACKNOWLEDGMENT = SEND_FEEDBACK_ACKNOWLEDGMENT
REQUEST_CAPABILITY_ANNOTATIONS = SEND_FEEDBACK_ANNOTATIONS
REQUEST_CAPABILITY_ARGUMENT_DESCRIPTION = SEND_FEEDBACK_ARGUMENT_DESCRIPTION

_NEW_KEYS = ("send_feedback", "sendFeedback")
# Deprecated aliases, read only when no new key is set.
_OLD_KEYS = ("request_capability", "requestCapability")


def _send_feedback_setting(config: AnalyticsConfig | None) -> Any:
    """The configured send_feedback value, or None when unset.

    The new key (``send_feedback``, camelCase ``sendFeedback``) wins over the
    deprecated ``request_capability`` / ``requestCapability`` alias whenever it
    is set. Between the two old spellings, False wins, as before.
    """
    armature = (config or {}).get("armature") or {}
    if not isinstance(armature, Mapping):
        return None
    for key in _NEW_KEYS:
        if armature.get(key) is not None:
            return armature[key]
    old = [armature.get(key) for key in _OLD_KEYS]
    if any(value is False for value in old):
        return False
    if any(value is True for value in old):
        return True
    return next((value for value in old if value is not None), None)


def send_feedback_enabled(config: AnalyticsConfig | None) -> bool:
    """True when the SDK registers its send_feedback tool.

    On by default when instrumentation is enabled and a delivery path (``emit``
    or an api key) is configured; ``send_feedback=False`` (or the deprecated
    ``request_capability=False``) turns it off. No other tool's description
    mentions it either way.
    """
    armature = (config or {}).get("armature") or {}
    if armature.get("enabled") is False:
        return False
    if _send_feedback_setting(config) is False:
        return False
    return callable(_config_value(config, "emit", "emit")) or bool(resolve_api_key(config))


def send_feedback_explicit(config: AnalyticsConfig | None) -> bool:
    """True only when the caller explicitly set send_feedback (or the
    deprecated alias) to True.

    Registration is governed by send_feedback_enabled (on unless disabled).
    The reserved-name and unsupported-server checks key off this stricter
    test: on merely by default, the SDK yields to a customer tool of the same
    name and skips quietly where it cannot register, instead of raising.
    """
    return _send_feedback_setting(config) is True


def send_feedback_registration() -> ToolRegistration:
    return {
        "name": SEND_FEEDBACK_TOOL_NAME,
        "description": SEND_FEEDBACK_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "description": SEND_FEEDBACK_ARGUMENT_DESCRIPTION,
                    "minLength": 1,
                    "maxLength": 1000,
                },
            },
            "required": ["capability"],
            "additionalProperties": False,
        },
        "annotations": dict(SEND_FEEDBACK_ANNOTATIONS),
    }


def acknowledge_feedback(args: Any = None, _context: Any = None) -> str:
    capability = args.get("capability") if isinstance(args, dict) else None
    if (
        not isinstance(capability, str)
        or not capability.strip()
        or len(capability) > 1000
    ):
        raise ValueError("capability must be a non-empty string of at most 1000 characters")
    return SEND_FEEDBACK_ACKNOWLEDGMENT


def send_feedback_reserved_error() -> ValueError:
    return ValueError(
        "Tool name 'send_feedback' is reserved while armature.send_feedback is True. "
        "Rename your tool or set send_feedback=False."
    )


# Deprecated aliases for the request_capability-era helpers.
request_capability_enabled = send_feedback_enabled
request_capability_explicit = send_feedback_explicit
request_capability_registration = send_feedback_registration
acknowledge_capability_request = acknowledge_feedback
