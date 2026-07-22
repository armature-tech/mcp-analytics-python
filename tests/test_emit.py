from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from armature_mcp_analytics.emit import (
    IngestRejectedError,
    create_flushable_emitter,
    detect_ingest_rejection,
)


class EmitTests(unittest.TestCase):
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


    def test_detect_ingest_rejection(self) -> None:
        # Explicit rejection, all-rejected, clean accept, dedup-only, skipped.
        self.assertIsInstance(
            detect_ingest_rejection({"skipped": False, "accepted": 0, "rejected": [{"reason": "persist_error"}]}, 1),
            IngestRejectedError,
        )
        self.assertIsInstance(
            detect_ingest_rejection({"skipped": False, "accepted": 0, "rejected": []}, 2), IngestRejectedError
        )
        self.assertIsNone(detect_ingest_rejection({"skipped": False, "accepted": 3, "rejected": []}, 3))
        self.assertIsNone(
            detect_ingest_rejection({"skipped": False, "accepted": 2, "rejected": [], "duplicate_count": 2}, 2)
        )
        self.assertIsNone(detect_ingest_rejection({"skipped": True}, 1))

    def test_emit_reports_in_body_rejection_through_on_error(self) -> None:
        # A 200 whose body rejects events must reach on_error (#1403).
        observed = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 200

            def read(self):
                return b'{"accepted":0,"rejected":[{"event_id":"e1","reason":"schema_version_mismatch"}]}'

        def on_error(error, _batch) -> None:
            observed.append(error)

        emitter = create_flushable_emitter(
            {"armature": {"delivery": "await", "api_key": "ami_test", "on_error": on_error}}
        )
        with mock.patch(
            "armature_mcp_analytics.emit.urllib.request.urlopen", return_value=Response()
        ):
            asyncio.run(emitter.emit_batch({"schema_version": 1, "events": [{"event_id": "e1"}]}))

        self.assertEqual(len(observed), 1)
        self.assertIsInstance(observed[0], IngestRejectedError)
        self.assertIn("schema_version_mismatch", str(observed[0]))

    def test_emit_stays_quiet_on_clean_accept(self) -> None:
        observed = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 200

            def read(self):
                return b'{"accepted":1,"rejected":[],"duplicate_count":0}'

        emitter = create_flushable_emitter(
            {"armature": {"delivery": "await", "api_key": "ami_test", "on_error": lambda e, b: observed.append(e)}}
        )
        with mock.patch(
            "armature_mcp_analytics.emit.urllib.request.urlopen", return_value=Response()
        ):
            asyncio.run(emitter.emit_batch({"schema_version": 1, "events": [{"event_id": "e1"}]}))

        self.assertEqual(observed, [])


if __name__ == "__main__":
    unittest.main()
