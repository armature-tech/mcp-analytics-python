from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .types import RedactFunction

# Placeholder strings are part of the cross-SDK contract
# (packages/TELEMETRY-CONTRACT.md) — golden tests in all three SDKs assert
# them byte-for-byte.
BINARY_REMOVED_PLACEHOLDER = "[binary removed]"
BASE64_REMOVED_PLACEHOLDER = "[base64 removed]"
REDACTION_FAILED_PLACEHOLDER = "[redaction failed]"

# A data: URI with a base64 payload is binary at any plausible size; plain
# strings need the higher bar (length + strict charset) so prose, ids, and
# hashes below half a KB pass through untouched. Both thresholds are contract
# values — keep in sync with the TypeScript and Go SDKs.
_DATA_URI_MIN_CHARS = 64
_BASE64_MIN_CHARS = 512

# Strict charset on purpose: no whitespace, so long prose (letters + spaces)
# never matches. Covers standard base64 and base64url, with optional padding.
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")


def _is_base64_payload(value: str) -> bool:
    if len(value) >= _DATA_URI_MIN_CHARS and value.startswith("data:") and ";base64," in value:
        return True
    return len(value) >= _BASE64_MIN_CHARS and _BASE64_RE.match(value) is not None


def sanitize_value(value: Any, _seen: set[int] | None = None) -> Any:
    """Recursively strips binary and base64 payloads from a tool input/output
    value before it is serialized into previews (gap #1). MCP image/audio
    content blocks lose their ``data``, resource blobs lose their ``blob``,
    and long base64 strings are replaced wholesale. Cycle-safe: a true cycle
    serializes as "[circular]" instead of recursing forever."""
    if isinstance(value, str):
        return BASE64_REMOVED_PLACEHOLDER if _is_base64_payload(value) else value
    if not isinstance(value, (Mapping, Sequence)) or isinstance(value, (bytes, bytearray)):
        return value

    seen = _seen if _seen is not None else set()
    marker = id(value)
    if marker in seen:
        return "[circular]"
    seen.add(marker)
    try:
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            block_type = value.get("type")
            for key, entry in value.items():
                if key == "data" and isinstance(entry, str) and block_type in ("image", "audio"):
                    out[key] = BINARY_REMOVED_PLACEHOLDER
                elif key == "blob" and isinstance(entry, str):
                    out[key] = BINARY_REMOVED_PLACEHOLDER
                else:
                    out[key] = sanitize_value(entry, seen)
            return out
        return [sanitize_value(item, seen) for item in value]
    finally:
        seen.discard(marker)


def prepare_for_preview(value: Any, redact: RedactFunction | None) -> Any:
    """sanitize → customer redact, failing closed: a raising hook replaces the
    whole payload with the placeholder rather than shipping unredacted data.
    The event itself still ships — losing a preview is recoverable, silently
    dropping calls from analytics is not."""
    sanitized = sanitize_value(value)
    if redact is None:
        return sanitized
    try:
        return redact(sanitized)
    except Exception:
        return REDACTION_FAILED_PLACEHOLDER
