from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from pipeline.runtime_logging import EVENT_SCHEMA_VERSION, MAX_EVENT_BYTES, StructuredLogger


class StructuredRuntimeLoggingTests(unittest.TestCase):
    def test_event_is_one_parseable_bounded_json_record_with_required_fields(self) -> None:
        stream = io.StringIO()
        with patch.dict("os.environ", {"RARDAR_RELEASE_SHA": "a" * 40}, clear=False):
            payload = StructuredLogger("scheduler", stream=stream).emit(
                "observation_completed",
                state="healthy",
                run_id="run-1",
                captureId="capture-1",
                candidateCount=20,
            )
        line = stream.getvalue()
        self.assertEqual(line.count("\n"), 1)
        parsed = json.loads(line)
        self.assertEqual(parsed, payload)
        for field in (
            "timestamp",
            "level",
            "service",
            "event",
            "eventSchemaVersion",
            "processId",
            "releaseSha",
            "runId",
            "state",
        ):
            self.assertIn(field, parsed)
        self.assertEqual(parsed["eventSchemaVersion"], EVENT_SCHEMA_VERSION)
        self.assertLessEqual(len(line.encode("utf-8")), MAX_EVENT_BYTES + 1)

    def test_secrets_authorization_paths_and_upstream_bodies_are_redacted(self) -> None:
        stream = io.StringIO()
        StructuredLogger("scheduler", stream=stream).emit(
            "observation_failed",
            state="failed",
            authorization="Bearer top-secret-token",
            apiKey="key-value",
            upstreamBody="private GitHub response body",
            message=(
                "Authorization: Bearer top-secret-token "
                "password=hunter2 C:\\private\\secret.txt /etc/rardar/rardar.secret "
                '{"token":"json-secret","cookie":"session-secret",'
                '"credential":"credential-secret"}'
            ),
            databaseUrl="postgres://user:pass@host/db",
        )
        line = stream.getvalue()
        for forbidden in (
            "top-secret-token",
            "key-value",
            "private GitHub response body",
            "hunter2",
            "secret.txt",
            "postgres://",
            "rardar.secret",
            "json-secret",
            "session-secret",
            "credential-secret",
        ):
            self.assertNotIn(forbidden, line)
        parsed = json.loads(line)
        self.assertEqual(parsed["authorization"], "[REDACTED]")
        self.assertEqual(parsed["upstreamBody"], "[REDACTED]")

    def test_unbounded_strings_and_metadata_failure_lists_are_truncated(self) -> None:
        stream = io.StringIO()
        StructuredLogger("scheduler", stream=stream).emit(
            "observation_completed",
            state="degraded",
            message="x" * 10_000,
            metadataFailureIds=list(range(1000)),
        )
        parsed = json.loads(stream.getvalue())
        self.assertLessEqual(len(parsed["message"]), 512)
        self.assertEqual(len(parsed["metadataFailureIds"]), 32)


if __name__ == "__main__":
    unittest.main()
