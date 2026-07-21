"""Unit coverage for HTTP header resolution on official-SDK servers.

`_http_headers_from_transport` must consult the official MCP SDK's
``request_ctx`` contextvar when fastmcp's accessors are unavailable (the
package is not installed) or see no fastmcp-managed HTTP request. These tests
drive the real contextvar directly — the transport-level behavior is pinned
end to end by test_e2e_official_sdk_http.py.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from armature_mcp_analytics import server as server_module
from armature_mcp_analytics.server import _context_from_call, _http_headers_from_transport

try:
    from mcp.server.lowlevel.server import request_ctx

    HAVE_MCP = True
except Exception:  # pragma: no cover - absence is a valid environment
    HAVE_MCP = False


class _NoFastmcpMixin(unittest.TestCase):
    """Run each test as if the standalone fastmcp package were absent."""

    def setUp(self) -> None:
        self._saved_fastmcp_deps = server_module._fastmcp_deps
        server_module._fastmcp_deps = None

    def tearDown(self) -> None:
        server_module._fastmcp_deps = self._saved_fastmcp_deps


def _official_sdk_context(request: object | None) -> SimpleNamespace:
    return SimpleNamespace(request_id="req-1", request=request)


@unittest.skipUnless(HAVE_MCP, "the official mcp package is required")
class OfficialSdkHeaderResolutionTests(_NoFastmcpMixin):
    def test_http_request_headers_surface_the_session_id(self) -> None:
        request = SimpleNamespace(headers={"mcp-session-id": "mcp_client_v_1.0_id"})
        token = request_ctx.set(_official_sdk_context(request))
        try:
            self.assertEqual(
                _http_headers_from_transport(),
                {"mcp-session-id": "mcp_client_v_1.0_id"},
            )
        finally:
            request_ctx.reset(token)

    def test_http_request_without_session_header_disarms_stdio_fallback(self) -> None:
        # {} (not None): an HTTP request IS active, so the recorder must not
        # glue the request onto the process-scoped stdio session id.
        request = SimpleNamespace(headers={"user-agent": "qa"})
        token = request_ctx.set(_official_sdk_context(request))
        try:
            self.assertEqual(_http_headers_from_transport(), {})
        finally:
            request_ctx.reset(token)

    def test_stdio_request_context_keeps_the_stdio_fallback(self) -> None:
        # Over stdio the official SDK sets a request context whose `request`
        # is None — that is the one shape allowed to reach the stdio fallback.
        token = request_ctx.set(_official_sdk_context(None))
        try:
            self.assertIsNone(_http_headers_from_transport())
        finally:
            request_ctx.reset(token)

    def test_no_active_request_context_keeps_the_stdio_fallback(self) -> None:
        self.assertIsNone(_http_headers_from_transport())

    def test_context_from_call_carries_official_sdk_headers(self) -> None:
        request = SimpleNamespace(headers={"mcp-session-id": "mcp_client_v_1.0_id"})
        token = request_ctx.set(_official_sdk_context(request))
        try:
            context = _context_from_call((), {})
            self.assertEqual(context.get("headers"), {"mcp-session-id": "mcp_client_v_1.0_id"})
        finally:
            request_ctx.reset(token)


class TransportCascadeTests(unittest.TestCase):
    def test_fastmcp_headers_win_when_fastmcp_serves_the_request(self) -> None:
        saved = server_module._fastmcp_deps
        server_module._fastmcp_deps = (
            lambda **_kwargs: {"mcp-session-id": "from-fastmcp"},
            lambda: SimpleNamespace(),
        )
        try:
            self.assertEqual(
                _http_headers_from_transport(),
                {"mcp-session-id": "from-fastmcp"},
            )
        finally:
            server_module._fastmcp_deps = saved

    def test_neither_stack_available_keeps_the_stdio_fallback(self) -> None:
        saved_fastmcp = server_module._fastmcp_deps
        saved_official = server_module._official_sdk_request_ctx
        server_module._fastmcp_deps = None
        server_module._official_sdk_request_ctx = None
        try:
            self.assertIsNone(_http_headers_from_transport())
        finally:
            server_module._fastmcp_deps = saved_fastmcp
            server_module._official_sdk_request_ctx = saved_official


if __name__ == "__main__":
    unittest.main()
