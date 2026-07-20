from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Pattern


@dataclass(frozen=True)
class SecretPatternRule:
    id: str
    pattern: Pattern[str]
    replacement: str


# Contract order is significant. Keep these expressions aligned with
# packages/TELEMETRY-CONTRACT.md and the TypeScript reference implementation.
SECRET_PATTERN_RULES: tuple[SecretPatternRule, ...] = (
    SecretPatternRule(
        "pem",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[redacted:pem]",
    ),
    SecretPatternRule(
        "sensitive-kv",
        re.compile(
            r"\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|authorization)([=:])([^\s\"'`,;&]{4,})",
            re.IGNORECASE | re.ASCII,
        ),
        r"\1\2[redacted:sensitive-kv]",
    ),
    SecretPatternRule(
        "aws-access-key-id",
        re.compile(
            r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA)[A-Z0-9]{16}\b",
            re.ASCII,
        ),
        "[redacted:aws-access-key-id]",
    ),
    SecretPatternRule(
        "github-token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,255})\b",
            re.ASCII,
        ),
        "[redacted:github-token]",
    ),
    SecretPatternRule(
        "google-api-key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b", re.ASCII),
        "[redacted:google-api-key]",
    ),
    SecretPatternRule(
        "slack-token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b", re.ASCII),
        "[redacted:slack-token]",
    ),
    SecretPatternRule(
        "stripe-key",
        re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b", re.ASCII),
        "[redacted:stripe-key]",
    ),
    SecretPatternRule(
        "anthropic-api-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b", re.ASCII),
        "[redacted:anthropic-api-key]",
    ),
    SecretPatternRule(
        "openai-api-key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b", re.ASCII),
        "[redacted:openai-api-key]",
    ),
    SecretPatternRule(
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{10,}\b",
            re.ASCII,
        ),
        "[redacted:jwt]",
    ),
    SecretPatternRule(
        "connection-string",
        re.compile(
            r"\b([a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+):([^\s@]+)@",
            re.ASCII,
        ),
        r"\1:[redacted:connection-string]@",
    ),
    SecretPatternRule(
        "bearer",
        re.compile(r"\b[Bb]earer +[A-Za-z0-9._~+/=-]{16,}", re.ASCII),
        "Bearer [redacted:bearer]",
    ),
    SecretPatternRule(
        "basic",
        re.compile(r"\b[Bb]asic +[A-Za-z0-9+/=]{16,}", re.ASCII),
        "Basic [redacted:basic]",
    ),
)

SENSITIVE_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "apikey",
        "accesskey",
        "secretkey",
        "secretaccesskey",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "authorization",
        "auth",
        "clientsecret",
        "privatekey",
        "credential",
        "credentials",
        "connectionstring",
        "databaseurl",
        "dsn",
    }
)


def normalize_sensitive_field_name(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def redact_secrets_in_string(value: str) -> str:
    redacted = value
    for rule in SECRET_PATTERN_RULES:
        redacted = rule.pattern.sub(rule.replacement, redacted)
    return redacted


def redact_secrets_in_value(value: Any, seen: set[int] | None = None) -> Any:
    if isinstance(value, str):
        return redact_secrets_in_string(value)
    if not isinstance(value, (dict, list)):
        return value

    tracked = seen if seen is not None else set()
    identity = id(value)
    if identity in tracked:
        return "[circular]"
    tracked.add(identity)
    try:
        if isinstance(value, list):
            return [redact_secrets_in_value(item, tracked) for item in value]

        output: dict[Any, Any] = {}
        for key, entry in value.items():
            output[key] = (
                "[redacted:sensitive-field]"
                if isinstance(key, str)
                and isinstance(entry, str)
                and normalize_sensitive_field_name(key) in SENSITIVE_FIELD_NAMES
                else redact_secrets_in_value(entry, tracked)
            )
        return output
    finally:
        tracked.remove(identity)
