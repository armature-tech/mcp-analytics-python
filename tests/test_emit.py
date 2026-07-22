from __future__ import annotations

import asyncio
import unittest
import urllib.error
from unittest.mock import patch

from armature_mcp_analytics.emit import (
    DEFAULT_TIMEOUT_MS,
    IngestDeliveryError,
    create_flushable_emitter,
    post_telemetry_event,
)


class EmitTests(unittest.TestCase):
    def test_default_timeout_is_five_seconds(self) -> None:
        self.assertEqual(DEFAULT_TIMEOUT_MS, 5_000)

    def test_transient_delivery_is_retried_once(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 202

        failure = urllib.error.HTTPError(
            "https://eu.armature.tech/api/mcp-analytics/ingest",
            503,
            "unavailable",
            {},
            None,
        )
        failure.read = lambda *_args: b'{"error":{"code":"temporarily_unavailable"}}'
        with patch("urllib.request.urlopen", side_effect=[failure, Response()]) as mocked:
            result = asyncio.run(post_telemetry_event(
                {"schema_version": 1, "events": []},
                {"armature": {"api_key": "ami_eu_test"}},
            ))
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(mocked.call_count, 2)

    def test_unauthorized_delivery_is_not_retried(self) -> None:
        failure = urllib.error.HTTPError(
            "https://eu.armature.tech/api/mcp-analytics/ingest",
            401,
            "unauthorized",
            {},
            None,
        )
        failure.read = lambda *_args: b'{"error":{"code":"ingest_key_wrong_region","message":"secret"}}'
        with patch("urllib.request.urlopen", side_effect=failure) as mocked:
            with self.assertRaises(IngestDeliveryError) as raised:
                asyncio.run(post_telemetry_event(
                    {"schema_version": 1, "events": []},
                    {"armature": {"api_key": "ami_us_test"}},
                ))
        self.assertEqual(raised.exception.code, "ingest_key_wrong_region")
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(raised.exception.retryable, False)
        self.assertEqual(raised.exception.attempts, 1)
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(mocked.call_count, 1)

    def test_network_failure_is_retried_once(self) -> None:
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")) as mocked:
            with self.assertRaises(IngestDeliveryError) as raised:
                asyncio.run(post_telemetry_event(
                    {"schema_version": 1, "events": []},
                    {"armature": {"api_key": "ami_us_test"}},
                ))
        self.assertEqual(raised.exception.code, "ingest_connection_failed")
        self.assertEqual(raised.exception.attempts, 2)
        self.assertEqual(mocked.call_count, 2)

    def test_timeout_is_retried_once(self) -> None:
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as mocked:
            with self.assertRaises(IngestDeliveryError) as raised:
                asyncio.run(post_telemetry_event(
                    {"schema_version": 1, "events": []},
                    {"armature": {"api_key": "ami_us_test", "timeout_ms": 5}},
                ))
        self.assertEqual(raised.exception.code, "ingest_timeout")
        self.assertEqual(raised.exception.attempts, 2)
        self.assertEqual(mocked.call_count, 2)

    def test_flush_waits_for_background_tasks(self) -> None:
        emitted = []

        async def emit(batch) -> None:
            await asyncio.sleep(0.01)
            emitted.append(batch)

        emitter = create_flushable_emitter({"armature": {"emit": emit}})
        batch = {"schema_version": 1, "events": []}

        async def run() -> None:
            await emitter.emit_batch(batch)
            self.assertEqual(emitted, [])
            await emitter.flush()

        asyncio.run(run())

        self.assertEqual(emitted, [batch])

    def test_on_error_callback_failures_do_not_escape_await_delivery(self) -> None:
        async def emit(_batch) -> None:
            raise RuntimeError("network failed")

        def on_error(_error, _batch) -> None:
            raise RuntimeError("callback failed")

        emitter = create_flushable_emitter(
            {"armature": {"delivery": "await", "emit": emit, "on_error": on_error}}
        )

        asyncio.run(emitter.emit_batch({"schema_version": 1, "events": []}))

    def test_async_on_error_callback_is_awaited(self) -> None:
        observed = []

        async def emit(_batch) -> None:
            raise RuntimeError("network failed")

        async def on_error(error, batch) -> None:
            await asyncio.sleep(0)
            observed.append((str(error), batch))

        batch = {"schema_version": 1, "events": []}
        emitter = create_flushable_emitter(
            {"armature": {"delivery": "await", "emit": emit, "on_error": on_error}}
        )

        asyncio.run(emitter.emit_batch(batch))

        self.assertEqual(observed, [("network failed", batch)])


if __name__ == "__main__":
    unittest.main()
