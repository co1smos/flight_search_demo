from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from flight_search_demo.app import GoogleGenAIRequestParser, run_request
from flight_search_demo.gemini_policy import (
    GeminiCallFailure,
    GeminiCallPolicy,
    GeminiErrorClassification,
    GeminiPolicyConfig,
)


class Issue14V9CorrectionTests(unittest.TestCase):
    def test_direct_parser_construction_rejects_equal_models_before_client_calls(self) -> None:
        client = Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(run_call_limit=1, daily_call_limits={"primary": 1}),
            )
            with self.assertRaisesRegex(ValueError, "models must be different"):
                GoogleGenAIRequestParser(
                    client=client, model="primary", fallback_model="primary", policy=policy,
                )
            self.assertEqual(client.mock_calls, [])
            self.assertEqual(policy.daily_usage("primary"), 0)
            self.assertEqual(policy.operation("construction").diagnostics()["remaining_run_calls"], 1)

    def test_deferred_initialization_lock_returns_classified_timeout_without_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "usage.sqlite3"
            policy = GeminiCallPolicy(
                db_path=db_path,
                config=GeminiPolicyConfig(run_call_limit=1, daily_call_limits={"primary": 1}),
                defer_initialization=True,
            )
            with contextlib.closing(sqlite3.connect(db_path)) as lock:
                lock.execute("BEGIN EXCLUSIVE")
                operation = policy.operation("locked-setup", request_id="request-setup", deadline_seconds=0.05)
                started = time.monotonic()
                try:
                    with self.assertRaises(GeminiCallFailure) as failure:
                        policy.initialize(operation)
                finally:
                    lock.rollback()
                elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.5)
            self.assertEqual(failure.exception.classification, GeminiErrorClassification.TIMEOUT_CANCELLATION)
            usage = failure.exception.diagnostics
            self.assertEqual(usage["terminal_classification"], "timeout_cancellation")
            self.assertIn("policy setup", usage["terminal_error"])
            self.assertEqual(usage["operation_id"], "locked-setup")
            self.assertEqual(usage["request_id"], "request-setup")
            self.assertEqual(usage["attempted_calls"], 0)
            self.assertEqual(usage["call_records"], [])
            self.assertEqual(usage["remaining_run_calls"], 1)
            self.assertEqual(usage["remaining_daily_calls"], {"primary": 1})
            self.assertEqual(policy.daily_usage("primary"), 0)

    def test_locked_initialization_emits_gemini_timeout_through_request_seam(self) -> None:
        client = Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            db_path = directory / "usage.sqlite3"
            event_path = directory / "events.jsonl"
            policy = GeminiCallPolicy(
                db_path=db_path,
                config=GeminiPolicyConfig(
                    run_call_limit=1, daily_call_limits={"primary": 1},
                    operation_deadline_seconds=0.05,
                ),
                defer_initialization=True,
            )
            parser = GoogleGenAIRequestParser(client=client, model="primary", policy=policy)
            with contextlib.closing(sqlite3.connect(db_path)) as lock:
                lock.execute("BEGIN EXCLUSIVE")
                started = time.monotonic()
                try:
                    with patch("flight_search_demo.app.build_default_request_parser", return_value=parser) as builder:
                        with contextlib.redirect_stdout(io.StringIO()):
                            events = run_request(
                                request={"request_id": "locked-request", "original_text": "find a flight"},
                                confirmation={"confirmed": False},
                                event_log_path=event_path,
                                current_date=date(2026, 11, 1),
                                gemini_policy=policy,
                                gemini_model="primary",
                            )
                finally:
                    lock.rollback()
                elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.5)
            builder.assert_not_called()
            self.assertEqual(client.mock_calls, [])
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["status"], "GEMINI_FAILED")
            self.assertEqual(json.loads(event_path.read_text()), event)
            usage = event["gemini_usage"]
            self.assertEqual(usage["terminal_classification"], "timeout_cancellation")
            self.assertEqual(usage["operation_id"], "request-parse:locked-request")
            self.assertEqual(usage["request_id"], "locked-request")
            self.assertEqual(usage["attempted_calls"], 0)
            self.assertEqual(usage["call_records"], [])
            self.assertEqual(usage["remaining_run_calls"], 1)
            self.assertEqual(usage["remaining_daily_calls"], {"primary": 1})
            self.assertEqual(usage["retry_decision"], "none")
            self.assertEqual(usage["fallback_decision"], "none")
            diagnostic = json.loads(event_path.with_suffix(".diagnostics.jsonl").read_text())
            self.assertEqual(diagnostic["diagnostic_id"], event["diagnostic_id"])
            self.assertEqual(diagnostic["metadata"], usage)
            self.assertEqual(policy.daily_usage("primary"), 0)


if __name__ == "__main__":
    unittest.main()
