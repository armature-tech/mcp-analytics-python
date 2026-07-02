from __future__ import annotations

import hashlib
import json
import os
import re
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from .types import Headers

SCHEMA_VERSION = 1
MAX_SOURCE_BYTES = 32 * 1024
MAX_PREVIEW_BYTES = 8 * 1024
MAX_CAPABILITIES_BYTES = 4 * 1024
WORKFLOW_RUN_ID_HEADER = "x-armature-workflow-run-id"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def read_env(key: str) -> str | None:
    return os.environ.get(key)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stringify_preview(value: Any) -> str:
    if value is None:
        return "null"
    try:
        return json.dumps(value, separators=(",", ":"), default=str)
    except Exception:
        return "[unserialisable]"


def truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def header_value(headers: Headers | None, name: str) -> str | None:
    if not headers:
        return None
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() != lower:
            continue
        if isinstance(value, (list, tuple)):
            first = value[0] if value else None
            return str(first) if first is not None else None
        return str(value) if value is not None else None
    return None


def workflow_run_id_from_headers(headers: Headers | None) -> str | None:
    raw = header_value(headers, WORKFLOW_RUN_ID_HEADER)
    value = raw.strip() if raw else ""
    return value if UUID_RE.match(value) else None


class BoundedKeySet:
    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        self._keys: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, key: str) -> bool:
        return key in self._keys

    def add(self, key: str) -> None:
        if key in self._keys:
            return
        while len(self._keys) >= self._max_entries:
            self._keys.popitem(last=False)
        self._keys[key] = None


def derive_tool_result_error(result: Any) -> str | None:
    if not isinstance(result, Mapping) or result.get("isError") is not True:
        return None
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if (
                isinstance(item, Mapping)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
                and item["text"].strip()
            ):
                return item["text"]
    return "tool returned isError"

