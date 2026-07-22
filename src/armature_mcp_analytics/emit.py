from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from typing import Any

from .types import ActorIdResolverInput, AnalyticsConfig, AnalyticsIngestBatch
from .utils import header_value, read_env

DEFAULT_ENDPOINT_URL = "https://app.armature.tech/api/mcp-analytics/ingest"
DEFAULT_TIMEOUT_MS = 5_000
DEFAULT_INGEST_MAX_ATTEMPTS = 2
DEFAULT_INGEST_RETRY_DELAY_SECONDS = 0.1
DEFAULT_USER_AGENT = "armature-mcp-analytics-python"


class IngestDeliveryError(RuntimeError):
    """Structured, payload-free diagnostic for a failed ingest delivery."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        attempts: int,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.attempts = attempts


def _response_error_code(error: urllib.error.HTTPError) -> str:
    fallback = f"ingest_http_{error.code}"
    try:
        body = error.read(4_096).decode("utf-8", errors="replace")
        payload = json.loads(body)
        nested = payload.get("error") if isinstance(payload, dict) else None
        candidate = (
            nested.get("code") if isinstance(nested, dict) else None
        ) or (payload.get("errorCode") if isinstance(payload, dict) else None)
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:-]{0,99}", candidate):
            return candidate
    except (AttributeError, TypeError, ValueError):
        pass
    return fallback


def _armature(config: AnalyticsConfig | None) -> dict[str, Any]:
    return dict((config or {}).get("armature") or {})


def _config_value(config: AnalyticsConfig | None, snake: str, camel: str, default: Any = None) -> Any:
    armature = _armature(config)
    if snake in armature:
        return armature[snake]
    if camel in armature:
        return armature[camel]
    return default


def resolve_endpoint_url(config: AnalyticsConfig | None = None) -> str:
    return (
        _config_value(config, "endpoint_url", "endpointUrl")
        or read_env("ANALYTICS_INGEST_URL")
        or DEFAULT_ENDPOINT_URL
    )


def resolve_api_key(config: AnalyticsConfig | None = None) -> str | None:
    return _config_value(config, "api_key", "apiKey") or read_env("ANALYTICS_INGEST_API_KEY")


async def resolve_actor_seed(config: AnalyticsConfig | None, input: ActorIdResolverInput) -> str:
    actor_id = _config_value(config, "actor_id", "actorId")
    if callable(actor_id):
        value = actor_id(input)
        if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
            value = await value
        return str(value)
    if actor_id:
        return str(actor_id)

    auth_info = input.get("authInfo") or {}
    for key in ("token", "clientId", "apiKey", "principalId"):
        value = auth_info.get(key)
        if value:
            return str(value)

    authorization = header_value(input.get("headers"), "authorization")
    if authorization:
        return authorization
    return "anonymous"


async def resolve_actor_identifier(
    config: AnalyticsConfig | None,
    input: ActorIdResolverInput,
) -> str | None:
    configured = _config_value(config, "actor_identifier", "actorIdentifier")
    if configured is None:
        return None
    value = configured(input) if callable(configured) else configured
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        value = await value
    if not isinstance(value, str) or not value:
        return None
    return value if len(value.encode("utf-8")) <= 8 * 1024 else None


async def post_telemetry_event(
    batch: AnalyticsIngestBatch,
    config: AnalyticsConfig | None = None,
) -> dict[str, Any]:
    api_key = resolve_api_key(config)
    if not api_key:
        return {"skipped": True, "reason": "ingest_config_missing"}

    body = json.dumps(batch, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        resolve_endpoint_url(config),
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # Cloudflare blocks Python's default urllib user agent (error 1010).
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="POST",
    )
    timeout_ms = _config_value(config, "timeout_ms", "timeoutMs", DEFAULT_TIMEOUT_MS)
    timeout = float(timeout_ms) / 1000

    def send() -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            return {"skipped": False, "ok": True, "status": status}

    for attempt in range(1, DEFAULT_INGEST_MAX_ATTEMPTS + 1):
        try:
            result = await asyncio.to_thread(send)
            return {**result, "attempts": attempt}
        except urllib.error.HTTPError as error:
            code = _response_error_code(error)
            retryable = error.code == 429 or error.code >= 500
            if retryable and attempt < DEFAULT_INGEST_MAX_ATTEMPTS:
                await asyncio.sleep(DEFAULT_INGEST_RETRY_DELAY_SECONDS)
                continue
            raise IngestDeliveryError(
                f"Armature ingest failed with HTTP {error.code} ({code})",
                code=code,
                status=error.code,
                retryable=retryable,
                attempts=attempt,
            ) from error
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", None)
            timed_out = isinstance(error, TimeoutError) or isinstance(reason, TimeoutError)
            if attempt < DEFAULT_INGEST_MAX_ATTEMPTS:
                await asyncio.sleep(DEFAULT_INGEST_RETRY_DELAY_SECONDS)
                continue
            code = "ingest_timeout" if timed_out else "ingest_connection_failed"
            raise IngestDeliveryError(
                "Armature ingest timed out" if timed_out else "Armature ingest connection failed",
                code=code,
                retryable=True,
                attempts=attempt,
            ) from error

    raise IngestDeliveryError(
        "Armature ingest delivery failed",
        code="ingest_delivery_failed",
        attempts=DEFAULT_INGEST_MAX_ATTEMPTS,
    )


class FlushableEmitter:
    def __init__(self, config: AnalyticsConfig | None = None) -> None:
        self.config = config or {}
        self._pending: set[asyncio.Task[None]] = set()

    def _enabled(self) -> bool:
        return _config_value(self.config, "enabled", "enabled", True) is not False

    def _delivery(self) -> str:
        return str(_config_value(self.config, "delivery", "delivery", "background"))

    def _emit_callable(self):
        return _config_value(self.config, "emit", "emit")

    def _on_error(self):
        return _config_value(self.config, "on_error", "onError")

    async def _run(self, batch: AnalyticsIngestBatch) -> None:
        try:
            emit = self._emit_callable()
            if emit:
                result = emit(batch)
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result
            else:
                await post_telemetry_event(batch, self.config)
        except Exception as error:
            # Telemetry delivery should not crash the host MCP server. Await
            # mode waits for the attempt to finish; failures are surfaced only
            # through the optional error hook, matching the JS SDK contract.
            on_error = self._on_error()
            if on_error:
                try:
                    result = on_error(error, batch)
                    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                        await result
                except Exception:
                    pass

    async def emit_batch(self, batch: AnalyticsIngestBatch) -> None:
        if not self._enabled():
            return
        if self._delivery() == "await":
            await self._run(batch)
            return
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._run(batch))
        self._pending.add(task)
        task.add_done_callback(lambda done: self._pending.discard(done))

    async def flush(self) -> None:
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)


def create_flushable_emitter(config: AnalyticsConfig | None = None) -> FlushableEmitter:
    return FlushableEmitter(config)
