from __future__ import annotations

import asyncio
import functools
import inspect
from dataclasses import dataclass
from typing import Annotated, Any

from .capability import (
    REQUEST_CAPABILITY_ACKNOWLEDGMENT,
    REQUEST_CAPABILITY_ARGUMENT_DESCRIPTION,
    REQUEST_CAPABILITY_DESCRIPTION,
    REQUEST_CAPABILITY_TOOL_NAME,
    request_capability_enabled,
    request_capability_registration,
)
from .recorder import AnalyticsRecorder, create_analytics_recorder
from .schema import (
    append_telemetry_hint,
    create_telemetry_json_schema,
    decorate_input_schema_with_telemetry,
    is_capture_enabled,
    schema_declares_telemetry,
    warn_telemetry_collision,
)
from .types import AnalyticsConfig, TelemetryMode

# Set on wrapper functions produced by instrument_fastmcp so re-entrant
# registrations can be recognized. fastmcp 2.x's `tool(name=...)` returns
# `partial(self.tool, ...)`, and `self.tool` re-reads the instance attribute we
# replaced — so the partial re-enters our decorator with the already-wrapped
# function. Without this marker that re-entry either double-instruments the
# tool or (kwargs present) returns the inner decorator instead of registering,
# silently dropping the tool from the server.
_ARMATURE_WRAPPED_MARKER = "__armature_mcp_analytics_wrapped__"


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


def _telemetry_annotation(config: AnalyticsConfig | None) -> Any:
    # FastMCP (2.x and 3.x) and the official SDK all build the advertised
    # inputSchema with pydantic from the tool function's type hints — any
    # input_schema kwarg we compute is never consulted (their tool() doesn't
    # accept one). A plain `dict | None` annotation therefore surfaces as a
    # bare anyOf[object, null] with none of the V1 field descriptions, and
    # agents never learn the telemetry fields exist. WithJsonSchema makes
    # pydantic advertise our full telemetry schema for the parameter while
    # still validating the value as a plain optional dict at call time.
    # The parameter keeps its None default because sparse telemetry is always
    # optional; user_intent is omitted after the first call in a user turn.
    annotation: Any = dict[str, Any] | None
    try:
        from pydantic import WithJsonSchema
    except Exception:
        # No pydantic → no schema-from-annotations server either; the
        # input_schema kwarg path (FakeFastMCP-style servers) still applies.
        return annotation
    return Annotated[annotation, WithJsonSchema(create_telemetry_json_schema(config))]


def _capability_annotation() -> Any:
    try:
        from pydantic import WithJsonSchema
    except Exception:
        return str
    return Annotated[
        str,
        WithJsonSchema(
            {
                "type": "string",
                "description": REQUEST_CAPABILITY_ARGUMENT_DESCRIPTION,
                "minLength": 1,
                "maxLength": 1000,
            }
        ),
    ]


def _resolved_signature(func: Any) -> inspect.Signature | None:
    # `from __future__ import annotations` in the customer's module leaves
    # every annotation as a string. The official SDK resolves those with
    # `inspect.signature(func, eval_str=True)` — but a precomputed
    # `__signature__` is returned verbatim, strings and all, so any signature
    # we attach to the wrapper must already carry evaluated annotations.
    # Otherwise the SDK's return-type detection sees a string, misclassifies
    # dict/BaseModel returns, and advertises a fallback outputSchema that
    # wraps results in {"result": ...} — changing customer-owned result
    # shapes, which the V1 wrapper-safety guarantee forbids (QA-03).
    try:
        return inspect.signature(func, eval_str=True)
    except (TypeError, ValueError):
        return None
    except Exception:
        # eval_str is all-or-nothing (e.g. NameError on a TYPE_CHECKING-only
        # name). Fall back to the unresolved signature rather than failing
        # tool registration.
        try:
            return inspect.signature(func)
        except (TypeError, ValueError):
            return None


def _signature_with_telemetry(func: Any, config: AnalyticsConfig | None = None) -> inspect.Signature | None:
    signature = _resolved_signature(func)
    if signature is None:
        return None
    if "telemetry" in signature.parameters:
        return signature

    telemetry = inspect.Parameter(
        "telemetry",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=_telemetry_annotation(config),
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


# Import-once cache for fastmcp's request-context accessors. The lookup runs
# on every tool call; re-importing (or re-raising ImportError when fastmcp is
# absent) per call would be needless overhead on hot paths.
_FASTMCP_DEPS_UNSET = object()
_fastmcp_deps: Any = _FASTMCP_DEPS_UNSET


def _load_fastmcp_deps() -> Any:
    global _fastmcp_deps
    if _fastmcp_deps is _FASTMCP_DEPS_UNSET:
        try:
            from fastmcp.server.dependencies import get_http_headers, get_http_request

            _fastmcp_deps = (get_http_headers, get_http_request)
        except Exception:
            _fastmcp_deps = None
    return _fastmcp_deps


def _http_headers_via_fastmcp() -> Any:
    # FastMCP never hands tool functions the transport context, so on HTTP
    # deployments pull the request headers from fastmcp's request-scoped
    # accessor. Returns None over stdio (no active HTTP request) or when
    # fastmcp is absent/too old. The recorder needs this distinction: requests
    # with headers (even zero of them) keep their real (or absent) session id,
    # while requests outside any HTTP context — stdio — fall back to the
    # process-scoped session id.
    deps = _load_fastmcp_deps()
    if deps is None:
        return None
    get_http_headers, get_http_request = deps
    # Presence of an HTTP request is the stdio/HTTP boundary — NOT header
    # emptiness. A pathological HTTP client could send only headers that
    # fastmcp's accessor strips, and coercing that empty dict to None would
    # glue every session on the server process to one stdio fallback id.
    try:
        request = get_http_request()
    except Exception:
        # RuntimeError: no active HTTP request → stdio / in-process.
        return None
    # Today fastmcp signals "no HTTP context" by raising; guard the return
    # value too in case a future version switches to returning None.
    if request is None:
        return None
    try:
        # `Mcp-Session-Id` is on fastmcp's default exclude list (it is meant
        # for proxy forwarding, where re-sending it would be wrong), but it IS
        # the session identity analytics needs — opt it back in.
        try:
            return get_http_headers(include={"mcp-session-id"})
        except TypeError:
            # fastmcp 2.x has no `include` — its only way past the exclude
            # list that strips Mcp-Session-Id is include_all=True. Filter back
            # down to the session header for parity with the 3.x branch.
            try:
                headers = get_http_headers(include_all=True)
            except TypeError:
                # fastmcp too old for either spelling: header-bearing requests
                # still stay out of the stdio fallback; sessionization falls
                # back to server-side bucketing as before.
                return get_http_headers()
            return {key: value for key, value in headers.items() if key.lower() == "mcp-session-id"}
    except Exception:
        # We KNOW an HTTP request is active; never degrade to the stdio
        # fallback just because header extraction failed.
        return {}


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
    if "headers" not in context:
        http_headers = _http_headers_via_fastmcp()
        # `is not None`, deliberately: an empty dict still means "an HTTP
        # request is active" and must keep the stdio fallback disarmed.
        if http_headers is not None:
            context["headers"] = http_headers
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


def _function_declares_telemetry(func: Any) -> bool:
    # The fastmcp path derives the advertised schema from the function
    # signature, so a customer function with its own `telemetry` parameter is
    # the signature-level equivalent of a schema that declares the property.
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    return "telemetry" in signature.parameters


def _wrap_handler(
    recorder: AnalyticsRecorder,
    name: str,
    func: Any,
    telemetry_mode: TelemetryMode = "injected",
    *,
    capability_request: bool = False,
):
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
                {
                    "name": name,
                    "args": raw_args,
                    "telemetry_mode": telemetry_mode,
                    "capability_request": capability_request,
                    **_context_from_call(args, kwargs),
                },
                lambda stripped: invoke_with_stripped_args(stripped, args, kwargs),
            )

        return async_wrapper

    @functools.wraps(func)
    async def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        raw_args = dict(kwargs)
        if args and isinstance(args[0], dict):
            raw_args = dict(args[0])
        return await recorder.instrument_tool_call(
            {
                "name": name,
                "args": raw_args,
                "telemetry_mode": telemetry_mode,
                "capability_request": capability_request,
                **_context_from_call(args, kwargs),
            },
            lambda stripped: invoke_with_stripped_args(stripped, args, kwargs),
        )

    return sync_wrapper


@dataclass
class FastMCPInstrumentation:
    server: Any
    recorder: AnalyticsRecorder


def _server_has_tool_named(server: Any, name: str) -> bool:
    candidates = [server, getattr(server, "_tool_manager", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        for attribute in ("tools", "_tools"):
            tools = getattr(candidate, attribute, None)
            if isinstance(tools, dict) and name in tools:
                return True
    return False


def instrument_fastmcp(server: Any, config: AnalyticsConfig | None = None) -> FastMCPInstrumentation:
    existing = getattr(server, "armature_analytics_instrumentation", None)
    if isinstance(existing, FastMCPInstrumentation):
        return existing

    recorder = create_analytics_recorder(config)
    original_tool = getattr(server, "tool", None)
    if not callable(original_tool):
        raise TypeError("instrument_fastmcp expects a FastMCP-like object with a callable .tool attribute.")
    supports_schema_kwargs = _supports_schema_kwargs(original_tool)

    if request_capability_enabled(config):
        if _server_has_tool_named(server, REQUEST_CAPABILITY_TOOL_NAME):
            raise ValueError(
                "Tool name 'request_capability' is reserved while "
                "armature.request_capability is enabled."
            )

        def request_capability(capability: str) -> str:
            if not capability.strip() or len(capability) > 1000:
                raise ValueError(
                    "capability must be a non-empty string of at most 1000 characters"
                )
            return REQUEST_CAPABILITY_ACKNOWLEDGMENT

        request_capability.__doc__ = REQUEST_CAPABILITY_DESCRIPTION
        wrapped_request_capability = _wrap_handler(
            recorder,
            REQUEST_CAPABILITY_TOOL_NAME,
            request_capability,
            "scrub",
            capability_request=True,
        )
        request_signature = inspect.signature(request_capability)
        capability_parameter = request_signature.parameters["capability"].replace(
            annotation=_capability_annotation()
        )
        wrapped_request_capability.__signature__ = request_signature.replace(
            parameters=[capability_parameter]
        )
        wrapped_request_capability.__annotations__ = {
            **getattr(wrapped_request_capability, "__annotations__", {}),
            "capability": capability_parameter.annotation,
        }
        capability_kwargs: dict[str, Any] = {
            "name": REQUEST_CAPABILITY_TOOL_NAME,
            "description": REQUEST_CAPABILITY_DESCRIPTION,
        }
        if supports_schema_kwargs:
            capability_kwargs["input_schema"] = request_capability_registration()["inputSchema"]
        original_tool(**capability_kwargs)(wrapped_request_capability)

    def instrumenting_tool(*decorator_args: Any, **decorator_kwargs: Any):
        # Re-entry guard: fastmcp 2.x's deferred registration comes back
        # through `self.tool` — which is now this function — carrying an
        # already-instrumented wrapper. Hand it straight to the original
        # registrar; wrapping again would record every call twice.
        if (
            decorator_args
            and callable(decorator_args[0])
            and getattr(decorator_args[0], _ARMATURE_WRAPPED_MARKER, False)
        ):
            return original_tool(*decorator_args, **decorator_kwargs)

        def decorate(func: Any):
            name = decorator_kwargs.get("name") or (decorator_args[0] if decorator_args and isinstance(decorator_args[0], str) else None) or func.__name__
            if (
                request_capability_enabled(config)
                and str(name) == REQUEST_CAPABILITY_TOOL_NAME
            ):
                raise ValueError(
                    "Tool name 'request_capability' is reserved while "
                    "armature.request_capability is enabled."
                )
            kwargs_schema = _schema_from_kwargs(decorator_kwargs)
            # Ownership (TELEMETRY-CONTRACT.md, mode "owned"): the customer's
            # function signature or explicit schema kwarg already declares
            # `telemetry` — never inject, strip, or interpret that field.
            if _function_declares_telemetry(func) or schema_declares_telemetry(kwargs_schema):
                telemetry_mode: TelemetryMode = "owned"
                warn_telemetry_collision(str(name))
            elif not is_capture_enabled(config):
                telemetry_mode = "scrub"
            else:
                telemetry_mode = "injected"

            kwargs = dict(decorator_kwargs)
            if telemetry_mode == "injected":
                schema = decorate_input_schema_with_telemetry(kwargs_schema, config)
                kwargs = _set_schema_kwargs(decorator_kwargs, schema, supports_schema_kwargs=supports_schema_kwargs)
                kwargs["description"] = append_telemetry_hint(_description_from(func, decorator_kwargs))
            wrapped = _wrap_handler(recorder, str(name), func, telemetry_mode)
            if telemetry_mode == "injected":
                wrapped_signature = _signature_with_telemetry(func, config)
                if wrapped_signature is not None:
                    wrapped.__signature__ = wrapped_signature
                    # Mirror the resolved signature into __annotations__ too:
                    # functools.wraps copied the customer function's dict,
                    # which under future annotations holds strings that would
                    # resolve against *our* module globals, not the customer's.
                    annotations = {
                        parameter_name: parameter.annotation
                        for parameter_name, parameter in wrapped_signature.parameters.items()
                        if parameter.annotation is not inspect.Parameter.empty
                    }
                    if wrapped_signature.return_annotation is not inspect.Signature.empty:
                        annotations["return"] = wrapped_signature.return_annotation
                    wrapped.__annotations__ = annotations
            # Marker must be set after _wrap_handler: functools.wraps copies
            # func.__dict__ onto wrapped, which would otherwise clobber it.
            setattr(wrapped, _ARMATURE_WRAPPED_MARKER, True)
            registration_args = decorator_args
            if registration_args and callable(registration_args[0]) and len(registration_args) == 1:
                registration_args = ()
            registered = original_tool(*registration_args, **kwargs)(wrapped)
            return registered

        # Bare `@tool` and direct `tool(fn, name=...)` calls both put the
        # function first; decorate() already folds decorator_kwargs in.
        if decorator_args and callable(decorator_args[0]) and len(decorator_args) == 1:
            return decorate(decorator_args[0])
        return decorate

    instrumentation = FastMCPInstrumentation(server=server, recorder=recorder)
    setattr(server, "tool", instrumenting_tool)
    setattr(server, "armature_analytics", recorder)
    setattr(server, "armature_analytics_instrumentation", instrumentation)
    return instrumentation


def with_mcp_analytics(server: Any, config: AnalyticsConfig | None = None) -> FastMCPInstrumentation:
    return instrument_fastmcp(server, config)
