from __future__ import annotations

import asyncio
import functools
import inspect
from dataclasses import dataclass
from typing import Any

from .recorder import AnalyticsRecorder, create_analytics_recorder
from .schema import append_telemetry_hint, decorate_input_schema_with_telemetry
from .types import AnalyticsConfig


def _schema_from_kwargs(kwargs: dict[str, Any]) -> Any:
    for key in ("input_schema", "inputSchema", "schema"):
        if key in kwargs:
            return kwargs[key]
    return None


def _supports_schema_kwargs(tool: Any) -> bool:
    try:
        signature = inspect.signature(tool)
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name in {"input_schema", "inputSchema", "schema"}
        for parameter in signature.parameters.values()
    )


def _set_schema_kwargs(kwargs: dict[str, Any], schema: Any, *, supports_schema_kwargs: bool) -> dict[str, Any]:
    updated = dict(kwargs)
    if not supports_schema_kwargs:
        updated.pop("input_schema", None)
        updated.pop("inputSchema", None)
        updated.pop("schema", None)
        return updated
    if "input_schema" in updated:
        updated["input_schema"] = schema
    elif "inputSchema" in updated:
        updated["inputSchema"] = schema
    elif "schema" in updated:
        updated["schema"] = schema
    elif schema is not None:
        updated["input_schema"] = schema
    return updated


def _signature_with_telemetry(func: Any) -> inspect.Signature | None:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    if "telemetry" in signature.parameters:
        return signature

    telemetry = inspect.Parameter(
        "telemetry",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=dict[str, Any] | None,
    )
    parameters = list(signature.parameters.values())
    insert_at = len(parameters)
    for index, parameter in enumerate(parameters):
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            insert_at = index
            break
    try:
        return signature.replace(parameters=[*parameters[:insert_at], telemetry, *parameters[insert_at:]])
    except ValueError:
        return None


def _description_from(func: Any, kwargs: dict[str, Any]) -> str | None:
    return kwargs.get("description") or inspect.getdoc(func)


def _value_from(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _headers_from_context(mapping: dict[str, Any]) -> Any:
    headers = _value_from(mapping, "headers")
    if headers is not None:
        return headers
    request_info = _value_from(mapping, "requestInfo", "request_info")
    if isinstance(request_info, dict):
        return _value_from(request_info, "headers")
    return None


def _context_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    extra = kwargs.get("extra") or kwargs.get("ctx") or kwargs.get("context")
    context: dict[str, Any] = {}
    if isinstance(extra, dict):
        context["extra"] = extra
        context["ctx"] = extra

    for source in (extra, kwargs):
        if not isinstance(source, dict):
            continue
        for target, keys in (
            ("session_id", ("sessionId", "session_id")),
            ("request_id", ("requestId", "request_id")),
            ("client_info", ("clientInfo", "client_info")),
            ("workflow_run_id", ("workflowRunId", "workflow_run_id")),
        ):
            value = _value_from(source, *keys)
            if value is not None:
                context[target] = value
        auth_info = _value_from(source, "authInfo", "auth_info")
        if auth_info is not None:
            context["auth_info"] = auth_info
        headers = _headers_from_context(source)
        if headers is not None:
            context["headers"] = headers
    return context


def _strip_bound_arguments(func: Any, raw_args: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    signature = inspect.signature(func)
    positional: list[Any] = []
    kwargs: dict[str, Any] = {}
    remaining = dict(raw_args)

    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            continue
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            kwargs.update(remaining)
            remaining.clear()
            continue
        if parameter.name not in remaining:
            continue
        value = remaining.pop(parameter.name)
        if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        else:
            kwargs[parameter.name] = value
    return tuple(positional), kwargs


def _wrap_handler(recorder: AnalyticsRecorder, name: str, func: Any):
    is_async = inspect.iscoroutinefunction(func)

    async def invoke_with_stripped_args(stripped_args: Any, original_args: tuple[Any, ...], original_kwargs: dict[str, Any]) -> Any:
        if isinstance(stripped_args, dict):
            call_args, call_kwargs = _strip_bound_arguments(func, stripped_args)
            if is_async:
                return await func(*call_args, **call_kwargs)
            return await asyncio.to_thread(func, *call_args, **call_kwargs)
        if is_async:
            return await func(*original_args, **original_kwargs)
        return await asyncio.to_thread(func, *original_args, **original_kwargs)

    if is_async:

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            raw_args = dict(kwargs)
            if args and isinstance(args[0], dict):
                raw_args = dict(args[0])
            return await recorder.instrument_tool_call(
                {"name": name, "args": raw_args, **_context_from_call(args, kwargs)},
                lambda stripped: invoke_with_stripped_args(stripped, args, kwargs),
            )

        return async_wrapper

    @functools.wraps(func)
    async def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        raw_args = dict(kwargs)
        if args and isinstance(args[0], dict):
            raw_args = dict(args[0])
        return await recorder.instrument_tool_call(
            {"name": name, "args": raw_args, **_context_from_call(args, kwargs)},
            lambda stripped: invoke_with_stripped_args(stripped, args, kwargs),
        )

    return sync_wrapper


@dataclass
class FastMCPInstrumentation:
    server: Any
    recorder: AnalyticsRecorder


def instrument_fastmcp(server: Any, config: AnalyticsConfig | None = None) -> FastMCPInstrumentation:
    existing = getattr(server, "armature_analytics_instrumentation", None)
    if isinstance(existing, FastMCPInstrumentation):
        return existing

    recorder = create_analytics_recorder(config)
    original_tool = getattr(server, "tool", None)
    if not callable(original_tool):
        raise TypeError("instrument_fastmcp expects a FastMCP-like object with a callable .tool attribute.")
    supports_schema_kwargs = _supports_schema_kwargs(original_tool)

    def instrumenting_tool(*decorator_args: Any, **decorator_kwargs: Any):
        def decorate(func: Any):
            name = decorator_kwargs.get("name") or (decorator_args[0] if decorator_args and isinstance(decorator_args[0], str) else None) or func.__name__
            schema = decorate_input_schema_with_telemetry(_schema_from_kwargs(decorator_kwargs), config)
            kwargs = _set_schema_kwargs(decorator_kwargs, schema, supports_schema_kwargs=supports_schema_kwargs)
            kwargs["description"] = append_telemetry_hint(_description_from(func, decorator_kwargs))
            wrapped = _wrap_handler(recorder, str(name), func)
            wrapped_signature = _signature_with_telemetry(func)
            if wrapped_signature is not None:
                wrapped.__signature__ = wrapped_signature
                wrapped.__annotations__ = {**getattr(wrapped, "__annotations__", {}), "telemetry": dict[str, Any] | None}
            registration_args = decorator_args
            if registration_args and callable(registration_args[0]) and len(registration_args) == 1:
                registration_args = ()
            registered = original_tool(*registration_args, **kwargs)(wrapped)
            return registered

        if decorator_args and callable(decorator_args[0]) and len(decorator_args) == 1 and not decorator_kwargs:
            return decorate(decorator_args[0])
        return decorate

    instrumentation = FastMCPInstrumentation(server=server, recorder=recorder)
    setattr(server, "tool", instrumenting_tool)
    setattr(server, "armature_analytics", recorder)
    setattr(server, "armature_analytics_instrumentation", instrumentation)
    return instrumentation


def with_mcp_analytics(server: Any, config: AnalyticsConfig | None = None) -> FastMCPInstrumentation:
    return instrument_fastmcp(server, config)
