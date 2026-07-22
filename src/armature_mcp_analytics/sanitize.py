from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .redact_secrets import redact_secrets_in_value

BINARY_REMOVED_PLACEHOLDER = "[binary removed]"
BASE64_REMOVED_PLACEHOLDER = "[base64 removed]"
REDACTION_FAILED_PLACEHOLDER = "[redaction failed]"
SANITIZATION_BUDGET = 65_536

_DATA_URI_MIN_CHARS = 64
_BASE64_MIN_CHARS = 512
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
# Base64-alphabet runs long enough to be payloads embedded inside a larger
# string, e.g. a blob echoed within a JSON-serialized tool result.
_EMBEDDED_BASE64_RE = re.compile(r"[A-Za-z0-9+/_-]{512,}={0,2}")


def _sanitize_string(value: str) -> str:
    if (
        len(value) >= _DATA_URI_MIN_CHARS
        and value.startswith("data:")
        and ";base64," in value
    ):
        return BASE64_REMOVED_PLACEHOLDER
    if len(value) >= _BASE64_MIN_CHARS:
        if _BASE64_RE.fullmatch(value):
            return BASE64_REMOVED_PLACEHOLDER
        return _EMBEDDED_BASE64_RE.sub(BASE64_REMOVED_PLACEHOLDER, value)
    return value


def _charge(budget: list[int], units: int) -> bool:
    if budget[0] < units:
        budget[0] = 0
        return False
    budget[0] -= units
    return True


def _sanitize_value_bounded(value: Any, seen: set[int], budget: list[int]) -> Any:
    if isinstance(value, str):
        # Bound pattern work to the retainable window first: previews are
        # truncated anyway, so scanning beyond the budget is pure waste on
        # large payloads.
        if len(value) > budget[0]:
            value = value[: budget[0]]
        sanitized = _sanitize_string(value)
        if len(sanitized) <= budget[0]:
            budget[0] -= len(sanitized)
            return sanitized
        sliced = sanitized[: budget[0]]
        budget[0] = 0
        return sliced
    if not isinstance(value, (dict, list)):
        return value

    identity = id(value)
    if identity in seen:
        return _sanitize_value_bounded("[circular]", seen, budget)
    seen.add(identity)
    try:
        if isinstance(value, list):
            output_list: list[Any] = []
            for item in value:
                if not _charge(budget, 2):
                    break
                output_list.append(_sanitize_value_bounded(item, seen, budget))
                if budget[0] == 0:
                    break
            return output_list

        output_dict: dict[Any, Any] = {}
        for key, entry in value.items():
            key_text = str(key)
            if not _charge(budget, len(key_text) + 2):
                break
            if (
                key == "data"
                and isinstance(entry, str)
                and value.get("type") in ("image", "audio")
            ) or (key == "blob" and isinstance(entry, str)):
                output_dict[key] = _sanitize_value_bounded(
                    BINARY_REMOVED_PLACEHOLDER, seen, budget
                )
            else:
                output_dict[key] = _sanitize_value_bounded(entry, seen, budget)
            if budget[0] == 0:
                break
        return output_dict
    finally:
        seen.remove(identity)


def sanitize_value(value: Any, seen: set[int] | None = None) -> Any:
    return _sanitize_value_bounded(
        value,
        seen if seen is not None else set(),
        [SANITIZATION_BUDGET],
    )


def prepare_for_preview(
    value: Any,
    redact: Callable[[Any], Any] | None = None,
    *,
    redact_secrets: bool = True,
) -> Any:
    sanitized = sanitize_value(value)
    protected = redact_secrets_in_value(sanitized) if redact_secrets else sanitized
    if redact is None:
        return protected
    try:
        return redact(protected)
    except Exception:
        return REDACTION_FAILED_PLACEHOLDER
