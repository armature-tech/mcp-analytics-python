from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Any

from .capability import (
    REQUEST_CAPABILITY_TOOL_NAME,
    acknowledge_capability_request,
    request_capability_enabled,
    request_capability_registration,
)
from .emit import _config_value, resolve_actor_identifier, resolve_actor_seed
from .events import (
    build_actor_id,
    build_actor_identity_event,
    build_batch,
    build_session_init_batch,
    finalize_tool_call_event,
    normalize_request_id,
    normalize_session_id,
    normalize_started_at,
    process_scoped_session_id,
)
from .queue import create_privacy_queue
from .schema import (
    apply_telemetry_field_map,
    extract_telemetry_arguments,
    is_capture_enabled,
    plan_tool_telemetry,
)
from .stateless_http import parse_stateless_session_client_info
from .types import (
    AnalyticsConfig,
    JsonDict,
    McpClientInfo,
    RequestExtra,
    TelemetryArgs,
    TelemetryMode,
    ToolRegistration,
)
from .utils import BoundedKeySet, derive_tool_result_error, header_value, workflow_run_id_from_headers


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _headers_from_extra(extra: RequestExtra | None) -> Any:
    request_info = (extra or {}).get("requestInfo")
    return request_info.get("headers") if isinstance(request_info, dict) else None


# Session id, in falling priority: explicit event/extra value, transport
# `Mcp-Session-Id` header, then — only for requests with no HTTP headers at
# all (stdio, in-process) — the process-scoped fallback. Stdio transports
# never carry a session id, and events shipped with `session_id_hint: None`
# get bucketed per-actor-per-day at ingest, merging distinct CLI conversations
# into one activity (see `process_scoped_session_id`). Requests that DO carry
# headers are excluded from the fallback: many sessions share a long-lived
# HTTP server process, so the absence of a session id there must stay visible
# to ingest instead of being glued to one process id.
def _resolve_session_id(
    session_id: str | None,
    extra: RequestExtra | None,
    headers: Any,
) -> str | None:
    normalized = normalize_session_id(session_id, extra)
    if normalized:
        return normalized
    effective_headers = headers if headers is not None else _headers_from_extra(extra)
    if effective_headers is None:
        return process_scoped_session_id()
    from_headers = header_value(effective_headers, "mcp-session-id")
    if isinstance(from_headers, str) and from_headers.strip():
        return from_headers.strip()
    return None


@dataclass
class _RegisteredTool:
    registration: ToolRegistration
    handler: Any
    telemetry_mode: TelemetryMode = "injected"
    decorate_with_telemetry: bool = True
    internal: bool = False


class AnalyticsRecorder:
    def __init__(self, config: AnalyticsConfig | None = None) -> None:
        self.config = config or {}
        self._privacy_queue = create_privacy_queue(self.config)
        self._session_init_keys = BoundedKeySet(10_000)
        self._tools: dict[str, _RegisteredTool] = {}
        self._pending_record_tasks: set[asyncio.Task[None]] = set()
        self._actor_identifiers: dict[str, str] = {}
        if request_capability_enabled(self.config):
            self._register_tool(
                request_capability_registration(),
                acknowledge_capability_request,
                telemetry_mode="scrub",
                decorate_with_telemetry=False,
                internal=True,
            )

    async def _analytics_context_for(
        self,
        *,
        ctx: Any = None,
        extra: RequestExtra | None = None,
        headers: Any = None,
        auth_info: JsonDict | None = None,
        tool_name: str | None = None,
        telemetry: TelemetryArgs | None = None,
    ) -> tuple[str, str | None]:
        resolver_input = {
            "ctx": ctx,
            "extra": extra,
            "headers": headers or _headers_from_extra(extra),
            "authInfo": auth_info or (extra or {}).get("authInfo") or {},
            "toolName": tool_name,
            "telemetry": telemetry or {},
        }
        identifier = await resolve_actor_identifier(self.config, resolver_input)
        seed = identifier if identifier is not None else await resolve_actor_seed(self.config, resolver_input)
        return build_actor_id(actor_seed=seed), identifier

    def _identity_event_for(self, actor_id: str, identifier: str | None, started_at: str):
        if identifier is None:
            return None
        if self._actor_identifiers.get(actor_id) == identifier:
            return None
        self._actor_identifiers[actor_id] = identifier
        if len(self._actor_identifiers) > 10_000:
            self._actor_identifiers.pop(next(iter(self._actor_identifiers)))
        return build_actor_identity_event(actor_id=actor_id, identifier=identifier, started_at=started_at)

    def _workflow_run_id(self, workflow_run_id: str | None, headers: Any, extra: RequestExtra | None) -> str | None:
        return workflow_run_id or workflow_run_id_from_headers(headers or _headers_from_extra(extra))

    def decorate_definitions(self, defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decorated: list[dict[str, Any]] = []
        for definition in defs:
            item = dict(definition)
            schema = item.get("inputSchema", item.get("input_schema"))
            plan = plan_tool_telemetry(
                str(item.get("name", "")),
                schema if schema is not None else {"type": "object", "properties": {}},
                self.config,
            )
            # Owned/scrub tools pass through undecorated — their advertised
            # schema and description must keep matching what the handler
            # actually receives.
            if plan.mode == "injected":
                item["description"] = plan.apply_description(item.get("description"))
            item["inputSchema"] = plan.input_schema
            item.pop("input_schema", None)
            decorated.append(item)
        return decorated

    def extract_telemetry(
        self,
        args: Any,
        mode: TelemetryMode = "injected",
    ) -> tuple[Any, TelemetryArgs | None]:
        return extract_telemetry_arguments(args, mode)

    async def record_session_init(
        self,
        *,
        session_id: str | None = None,
        ctx: Any = None,
        extra: RequestExtra | None = None,
        headers: Any = None,
        auth_info: JsonDict | None = None,
        started_at: str | int | float | None = None,
        client_info: McpClientInfo | None = None,
        workflow_run_id: str | None = None,
    ) -> None:
        normalized_session_id = _resolve_session_id(session_id, extra, headers)
        if client_info is None:
            client_info = parse_stateless_session_client_info(normalized_session_id)
        if not normalized_session_id:
            return
        started = normalize_started_at(started_at)
        effective_workflow_run_id = self._workflow_run_id(workflow_run_id, headers, extra)

        async def finalize() -> list[dict[str, Any]] | None:
            actor_id, actor_identifier = await self._analytics_context_for(
                ctx=ctx, extra=extra, headers=headers, auth_info=auth_info
            )
            batch = build_session_init_batch(
                actor_id=actor_id,
                session_id=normalized_session_id,
                started_at=started,
                extra=extra,
                session_init_keys=self._session_init_keys,
                client_info=client_info,
                workflow_run_id=effective_workflow_run_id,
                identity_event=self._identity_event_for(actor_id, actor_identifier, started),
            )
            return None if batch is None else batch["events"]

        await self._privacy_queue.enqueue(finalize)  # type: ignore[arg-type]

    async def record_tool_call(
        self,
        *,
        name: str,
        args: Any = None,
        telemetry: TelemetryArgs | None = None,
        ctx: Any = None,
        extra: RequestExtra | None = None,
        headers: Any = None,
        auth_info: JsonDict | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        started_at: str | int | float | None = None,
        duration_ms: int = 0,
        status: str,
        result: Any = None,
        error: Any = None,
        client_info: McpClientInfo | None = None,
        workflow_run_id: str | None = None,
        capability_request: bool = False,
    ) -> None:
        # Single choke point for capture-off and field ownership
        # (TELEMETRY-CONTRACT.md): telemetry handed in by any path —
        # extraction, direct record_tool_call callers, a cached-schema client —
        # is dropped here before it can reach the actor resolver, the event
        # builder, `emit`, or `on_error`. A registered tool that owns its
        # telemetry field never exports supplied telemetry either; the opt-in
        # field map is the explicit way to export customer fields, and it only
        # applies while capture is on.
        if is_capture_enabled(self.config):
            registered = self._tools.get(name)
            if registered is not None and registered.telemetry_mode == "owned":
                telemetry = None
            telemetry = apply_telemetry_field_map(
                telemetry,
                args,
                _config_value(self.config, "telemetry_field_map", "telemetryFieldMap"),
            )
        else:
            telemetry = None

        finished_ms = time.time() * 1000
        started = normalize_started_at(started_at, duration_ms=duration_ms, finished_at_ms=finished_ms)
        finished = normalize_started_at(finished_ms)
        normalized_session_id = _resolve_session_id(session_id, extra, headers)
        if client_info is None:
            client_info = parse_stateless_session_client_info(normalized_session_id)
        error_message = None if error is None else str(error)
        effective_workflow_run_id = self._workflow_run_id(workflow_run_id, headers, extra)
        normalized_request_id = normalize_request_id(request_id)

        async def finalize() -> list[dict[str, Any]] | None:
            actor_id, actor_identifier = await self._analytics_context_for(
                ctx=ctx,
                extra=extra,
                headers=headers,
                auth_info=auth_info,
                tool_name=name,
                telemetry=telemetry,
            )
            event = await finalize_tool_call_event(
                tool_name=name,
                telemetry=telemetry,
                input=args,
                output=result,
                status=status,
                duration_ms=duration_ms,
                error_message=error_message,
                actor_id=actor_id,
                session_id=normalized_session_id,
                request_id=normalized_request_id,
                started_at=started,
                finished_at=finished,
                workflow_run_id=effective_workflow_run_id,
                capability_request=capability_request,
                redact=_config_value(self.config, "redact", "redact"),
                redact_secrets=_config_value(self.config, "redact_secrets", "redactSecrets", True) is not False,
                redact_event=_config_value(self.config, "redact_event", "redactEvent"),
            )
            identity_event = self._identity_event_for(actor_id, actor_identifier, started)
            effective_extra = {
                **(extra or {}),
                **({"sessionId": normalized_session_id} if normalized_session_id else {}),
            }
            if event is not None:
                return build_batch(
                    event=event,
                    extra=effective_extra,
                    actor_id=actor_id,
                    started_at=started,
                    session_init_keys=self._session_init_keys,
                    client_info=client_info,
                    workflow_run_id=effective_workflow_run_id,
                    identity_event=identity_event,
                )["events"]
            if normalized_session_id:
                batch = build_session_init_batch(
                    actor_id=actor_id,
                    session_id=normalized_session_id,
                    started_at=started,
                    extra=effective_extra,
                    session_init_keys=self._session_init_keys,
                    client_info=client_info,
                    workflow_run_id=effective_workflow_run_id,
                    identity_event=identity_event,
                )
                return None if batch is None else batch["events"]
            return [identity_event] if identity_event is not None else None

        await self._privacy_queue.enqueue(finalize)  # type: ignore[arg-type]

    async def instrument_tool_call(self, event: dict[str, Any], handler: Any) -> Any:
        args, telemetry = extract_telemetry_arguments(
            event.get("args"),
            event.get("telemetry_mode") or "injected",
        )
        started = time.time()
        started_at = normalize_started_at()
        try:
            result = await _maybe_await(handler(args))
        except BaseException as error:
            record_error = self.record_tool_call(
                **{k: v for k, v in event.items() if k not in ("args", "telemetry_mode")},
                args=args,
                telemetry=telemetry,
                started_at=started_at,
                duration_ms=int((time.time() - started) * 1000),
                status="error",
                error=error,
            )
            if isinstance(error, asyncio.CancelledError):
                task = asyncio.create_task(record_error)
                self._pending_record_tasks.add(task)

                def consume_record_task(done: asyncio.Task[None]) -> None:
                    self._pending_record_tasks.discard(done)
                    if not done.cancelled():
                        done.exception()

                task.add_done_callback(consume_record_task)
            else:
                try:
                    await record_error
                except BaseException:
                    pass
            raise

        result_error = derive_tool_result_error(result)
        try:
            await self.record_tool_call(
                **{k: v for k, v in event.items() if k not in ("args", "telemetry_mode")},
                args=args,
                telemetry=telemetry,
                started_at=started_at,
                duration_ms=int((time.time() - started) * 1000),
                status="ok" if result_error is None else "error",
                result=result,
                error=result_error,
            )
        except BaseException:
            pass
        return result

    def _register_tool(
        self,
        registration: ToolRegistration,
        handler: Any,
        *,
        telemetry_mode: TelemetryMode,
        decorate_with_telemetry: bool,
        internal: bool = False,
    ) -> None:
        name = registration["name"]
        self._tools[name] = _RegisteredTool(
            registration=registration,
            handler=handler,
            telemetry_mode=telemetry_mode,
            decorate_with_telemetry=decorate_with_telemetry,
            internal=internal,
        )

    def tool(self, registration: ToolRegistration, handler: Any):
        name = registration["name"]
        if request_capability_enabled(self.config) and name == REQUEST_CAPABILITY_TOOL_NAME:
            raise ValueError(
                "Tool name 'request_capability' is reserved while "
                "armature.request_capability is enabled."
            )
        schema = registration.get("inputSchema", registration.get("input_schema"))
        self._register_tool(
            registration,
            handler,
            telemetry_mode=plan_tool_telemetry(name, schema, self.config).mode,
            decorate_with_telemetry=True,
        )

        async def dispatch_registered(raw_args: Any, context: dict[str, Any] | None = None) -> Any:
            return await self.dispatch(name, raw_args, context or {})

        return dispatch_registered

    async def dispatch(self, name: str, raw_args: Any, context: dict[str, Any] | None = None) -> Any:
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"Unknown tool: {name}")
        context = context or {}

        async def handler(stripped_args: Any) -> Any:
            return await _maybe_await(tool.handler(stripped_args, context))

        return await self.instrument_tool_call(
            {
                "name": name,
                "args": raw_args,
                "telemetry_mode": tool.telemetry_mode,
                "ctx": context.get("ctx"),
                "extra": context.get("extra"),
                "headers": context.get("headers"),
                "auth_info": context.get("authInfo") or context.get("auth_info"),
                "session_id": context.get("sessionId") or context.get("session_id"),
                "request_id": context.get("requestId") or context.get("request_id"),
                "client_info": context.get("clientInfo") or context.get("client_info"),
                "workflow_run_id": context.get("workflowRunId") or context.get("workflow_run_id"),
                "capability_request": tool.internal,
            },
            handler,
        )

    def tool_definitions(self) -> list[dict[str, Any]]:
        defs: list[dict[str, Any]] = []
        for tool in self._tools.values():
            registration = tool.registration
            definition = {"name": registration["name"]}
            for key in ("title", "description", "inputSchema"):
                if key in registration:
                    definition[key] = registration[key]
            if "input_schema" in registration:
                definition["inputSchema"] = registration["input_schema"]
            if tool.decorate_with_telemetry:
                defs.extend(self.decorate_definitions([definition]))
            else:
                defs.append(definition)
        return defs

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    async def flush(self) -> None:
        while self._pending_record_tasks:
            await asyncio.gather(*list(self._pending_record_tasks), return_exceptions=True)
        await self._privacy_queue.flush()


def create_analytics_recorder(config: AnalyticsConfig | None = None) -> AnalyticsRecorder:
    return AnalyticsRecorder(config)
