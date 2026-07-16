from __future__ import annotations

import json
import unittest

from armature_mcp_analytics import (
    StatelessHttpSessionMiddleware,
    build_stateless_session_id,
    create_analytics_recorder,
    parse_stateless_session_client_info,
    resolve_stateless_http_session,
)


class StatelessHttpTests(unittest.IsolatedAsyncioTestCase):
    def test_session_id_round_trips_client_identity(self) -> None:
        session_id = build_stateless_session_id(
            {"name": "Claude Code", "version": "2.0.13"}
        )
        self.assertRegex(session_id, r"^mcp_Claude-Code_v_2\.0\.13_[0-9a-f-]{36}$")
        self.assertEqual(
            parse_stateless_session_client_info(session_id),
            {"name": "Claude-Code", "version": "2.0.13"},
        )

    def test_anonymous_and_malformed_ids_do_not_claim_identity(self) -> None:
        anonymous = build_stateless_session_id()
        self.assertTrue(anonymous.startswith("mcp_unknown_v__"))
        self.assertIsNone(parse_stateless_session_client_info(anonymous))
        self.assertIsNone(parse_stateless_session_client_info("session_123"))
        self.assertIsNone(parse_stateless_session_client_info(None))

    def test_initialize_mints_identity_id_and_generator(self) -> None:
        session = resolve_stateless_http_session(
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "cursor", "version": "1.5"}},
            },
            headers={},
        )
        self.assertTrue(session.is_initialize)
        self.assertTrue(session.session_id.startswith("mcp_cursor_v_1.5_"))
        self.assertEqual(session.session_id_generator(), session.session_id)
        self.assertEqual(session.dispatch_context, {"sessionId": session.session_id})

    def test_initialize_inside_batch_is_detected(self) -> None:
        session = resolve_stateless_http_session(
            body=[
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "vscode"}},
                },
            ]
        )
        self.assertTrue(session.is_initialize)
        self.assertTrue(session.session_id.startswith("mcp_vscode_v__"))

    def test_later_request_recovers_echoed_identity_case_insensitively(self) -> None:
        issued = build_stateless_session_id(
            {"name": "claude-code", "version": "2.0.13"}
        )
        session = resolve_stateless_http_session(
            body={"jsonrpc": "2.0", "id": 2, "method": "tools/call"},
            headers={"MCP-Session-ID": issued},
        )
        self.assertFalse(session.is_initialize)
        self.assertEqual(session.session_id, issued)
        self.assertIsNone(session.session_id_generator)
        self.assertEqual(
            session.dispatch_context,
            {
                "sessionId": issued,
                "clientInfo": {"name": "claude-code", "version": "2.0.13"},
            },
        )

    def test_missing_echo_falls_back_to_one_off_uuid(self) -> None:
        session = resolve_stateless_http_session(
            body={"method": "tools/call"}, headers={}
        )
        self.assertRegex(session.session_id, r"^[0-9a-f-]{36}$")
        self.assertIsNone(session.client_info)

    async def test_recorder_recovers_client_identity_from_session_id(self) -> None:
        batches = []
        recorder = create_analytics_recorder(
            {
                "armature": {
                    "delivery": "await",
                    "actor_id": "actor",
                    "emit": batches.append,
                }
            }
        )
        session_id = build_stateless_session_id(
            {"name": "claude-code", "version": "2.0.13"}
        )
        await recorder.record_tool_call(
            name="lookup_customer",
            args={},
            session_id=session_id,
            request_id="request_1",
            duration_ms=1,
            status="ok",
            result={"content": [{"type": "text", "text": "ok"}]},
        )
        events = [event for batch in batches for event in batch["events"]]
        session_init = next(
            event for event in events if event["kind"] == "session_init"
        )
        self.assertEqual(session_init["metadata"]["client_name"], "claude-code")
        self.assertEqual(session_init["metadata"]["client_version"], "2.0.13")

    async def test_asgi_middleware_issues_and_propagates_session_identity(self) -> None:
        seen_scopes = []

        async def app(scope, receive, send):
            seen_scopes.append(scope)
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"jsonrpc":"2.0","id":1,"result":{}}',
                }
            )

        middleware = StatelessHttpSessionMiddleware(app)

        async def invoke(body, headers=()):
            incoming = [
                {
                    "type": "http.request",
                    "body": json.dumps(body).encode(),
                    "more_body": False,
                }
            ]
            outgoing = []

            async def receive():
                return incoming.pop(0)

            async def send(message):
                outgoing.append(message)

            await middleware(
                {"type": "http", "method": "POST", "headers": list(headers)},
                receive,
                send,
            )
            return outgoing

        initialized = await invoke(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "claude-code", "version": "9.9.9"}},
            }
        )
        session_id = dict(initialized[0]["headers"])[b"mcp-session-id"].decode()
        self.assertTrue(session_id.startswith("mcp_claude-code_v_9.9.9_"))

        await invoke(
            {"method": "tools/call"},
            headers=[(b"mcp-session-id", session_id.encode())],
        )
        self.assertEqual(
            dict(seen_scopes[-1]["headers"])[b"mcp-session-id"].decode(), session_id
        )

        await invoke({"method": "tools/call"})
        fallback = dict(seen_scopes[-1]["headers"])[b"mcp-session-id"].decode()
        self.assertRegex(fallback, r"^[0-9a-f-]{36}$")

    async def test_failed_initialize_does_not_issue_session_id(self) -> None:
        async def app(_scope, receive, send):
            await receive()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"mcp-session-id", b"transport-generated")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": (
                        b'{"jsonrpc":"2.0","id":1,"error":'
                        b'{"code":-32602,"message":"invalid initialize"}}'
                    ),
                }
            )

        incoming = [
            {
                "type": "http.request",
                "body": json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {},
                    }
                ).encode(),
                "more_body": False,
            }
        ]
        outgoing = []

        async def receive():
            return incoming.pop(0)

        async def send(message):
            outgoing.append(message)

        await StatelessHttpSessionMiddleware(app)(
            {"type": "http", "method": "POST", "headers": []},
            receive,
            send,
        )
        self.assertNotIn(b"mcp-session-id", dict(outgoing[0]["headers"]))

    async def test_real_fastmcp_stateless_app_keeps_one_session(self) -> None:
        try:
            from fastmcp import FastMCP
            from starlette.testclient import TestClient
        except ImportError:
            self.skipTest("fastmcp optional dependency is not installed")

        batches = []
        mcp = FastMCP("stateless-python-e2e")
        from armature_mcp_analytics import instrument_fastmcp

        instrument_fastmcp(
            mcp, {"armature": {"delivery": "await", "emit": batches.append}}
        )

        @mcp.tool
        def echo(text: str) -> str:
            return text

        app = StatelessHttpSessionMiddleware(
            mcp.http_app(stateless_http=True, json_response=True)
        )
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        with TestClient(app, base_url="http://localhost:8000") as client:
            initialized = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "parity-client", "version": "9.9.9"},
                    },
                },
            )
            self.assertEqual(initialized.status_code, 200)
            session_id = initialized.headers["mcp-session-id"]
            self.assertTrue(session_id.startswith("mcp_parity-client_v_9.9.9_"))
            session_headers = {**headers, "mcp-session-id": session_id}
            client.post(
                "/mcp",
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            called = client.post(
                "/mcp",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "stateless"}},
                },
            )
            self.assertEqual(called.status_code, 200)

        events = [event for batch in batches for event in batch["events"]]
        self.assertTrue(any(event["kind"] == "tool_call" for event in events))
        self.assertEqual({event["session_id_hint"] for event in events}, {session_id})
        session_init = next(
            event for event in events if event["kind"] == "session_init"
        )
        self.assertEqual(session_init["metadata"]["client_name"], "parity-client")
        self.assertEqual(session_init["metadata"]["client_version"], "9.9.9")

    async def test_official_sdk_stateless_app_keeps_one_session(self) -> None:
        try:
            from mcp.server.fastmcp import FastMCP
            from starlette.testclient import TestClient
        except ImportError:
            self.skipTest("mcp optional dependency is not installed")

        from armature_mcp_analytics import instrument_fastmcp

        batches = []
        mcp = FastMCP(
            "official-python-stateless-e2e",
            stateless_http=True,
            json_response=True,
        )
        instrument_fastmcp(
            mcp, {"armature": {"delivery": "await", "emit": batches.append}}
        )

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        app = StatelessHttpSessionMiddleware(mcp.streamable_http_app())
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        with TestClient(app, base_url="http://localhost:8000") as client:
            initialized = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "official-client", "version": "8.8.8"},
                    },
                },
            )
            self.assertEqual(initialized.status_code, 200)
            session_id = initialized.headers["mcp-session-id"]
            session_headers = {**headers, "mcp-session-id": session_id}
            called = client.post(
                "/mcp",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "stateless"}},
                },
            )
            self.assertEqual(called.status_code, 200)

        events = [event for batch in batches for event in batch["events"]]
        self.assertTrue(any(event["kind"] == "tool_call" for event in events))
        self.assertEqual({event["session_id_hint"] for event in events}, {session_id})
        session_init = next(
            event for event in events if event["kind"] == "session_init"
        )
        self.assertEqual(session_init["metadata"]["client_name"], "official-client")
        self.assertEqual(session_init["metadata"]["client_version"], "8.8.8")


if __name__ == "__main__":
    unittest.main()
