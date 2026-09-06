from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flight_search_demo.app import (  # noqa: E402
    NormalizedCriteria,
    ParserFailure,
    ParsedNaturalLanguageRequest,
    RequestParseResult,
    build_default_request_parser,
    main,
    run_request,
)
from flight_search_demo.gemini_policy import (  # noqa: E402
    GeminiCallPolicy,
    GeminiPolicyConfig,
    redact_sensitive_text,
)


class Issue14V8CorrectionTests(unittest.TestCase):
    def test_serialized_sensitive_mapping_containers_redact_complete_values(self) -> None:
        samples = (
            "headers={'Cookie': ['sid=COOKIE_SECRET', 'csrf=CSRF_SECRET'], 'keep': 'keep-me'}",
            'headers={"Cookie": ("sid=COOKIE_SECRET", "csrf=CSRF_SECRET"), "keep": "keep-me"}',
            "headers={'X-Api-Key': {'current': 'API_SECRET', 'old': 'OLD_SECRET'}, 'keep': 'keep-me'}",
            "headers={'credential': ('CREDENTIAL_SECRET',), 'password': 'PASSWORD_SECRET', 'keep': 'keep-me'}",
        )
        for sample in samples:
            with self.subTest(sample=sample.split(":", 1)[0]):
                safe = redact_sensitive_text(sample)
                for secret in (
                    "COOKIE_SECRET", "CSRF_SECRET", "API_SECRET", "OLD_SECRET",
                    "CREDENTIAL_SECRET", "PASSWORD_SECRET",
                ):
                    self.assertNotIn(secret, safe)
                self.assertIn("keep-me", safe)

    def test_serialized_container_redaction_reaches_persisted_application_diagnostics(self) -> None:
        samples = {
            "cookie": "headers={'Cookie': ['sid=COOKIE_SECRET', 'csrf=CSRF_SECRET'], 'keep': 'keep-me'}",
            "tuple_cookie": 'headers={"Cookie": ("sid=TUPLE_SECRET", "csrf=TUPLE_CSRF_SECRET")}',
            "api_key": "headers={'X-Api-Key': {'current': 'API_SECRET'}, 'keep': 'keep-me'}",
        }

        class FailingParser:
            def parse(self, **kwargs):
                raise ParserFailure(
                    "serialized diagnostics",
                    diagnostics={"serialized": " | ".join(samples.values()), "ordinary": "keep-me"},
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            run_request(
                request={"request_id": "serialized-redaction", "original_text": "find a flight"},
                confirmation={"confirmed": False},
                event_log_path=directory / "events.jsonl",
                parser=FailingParser(),
            )
            persisted = (directory / "events.diagnostics.jsonl").read_text(encoding="utf-8")

        for secret in (
            "COOKIE_SECRET", "CSRF_SECRET", "TUPLE_SECRET", "TUPLE_CSRF_SECRET", "API_SECRET",
        ):
            self.assertNotIn(secret, persisted)
        self.assertIn("keep-me", persisted)

    def test_default_natural_language_setup_passes_one_operation_deadline_to_setup_and_parse(self) -> None:
        calls: list[str] = []

        class FakeOperation:
            def remaining_seconds(self):
                return 10.0

            def run_sync(self, callback):
                calls.append("setup")
                return callback()

        operation = FakeOperation()

        class Parser:
            def parse(self, **kwargs):
                self.operation = kwargs["operation"]
                calls.append("parse")
                return RequestParseResult([], program_selection="ambiguous")

        parser = Parser()

        def builder(*, policy, operation, **kwargs):
            calls.append("build")
            self.assertIs(kwargs_policy, policy)
            self.assertIs(operation, operation_ref)
            return parser

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(daily_call_limits={"primary": 1}),
                defer_initialization=True,
            )
            kwargs_policy = policy
            operation_ref = operation
            with patch("flight_search_demo.app.build_default_request_parser", side_effect=builder):
                result = run_request(
                    request={"request_id": "setup-deadline", "original_text": "find a flight"},
                    confirmation={"confirmed": False},
                    event_log_path=Path(tmpdir) / "events.jsonl",
                    gemini_policy=policy,
                    gemini_operation=operation,
                )

        self.assertEqual(result[0]["status"], "CLARIFICATION_REQUIRED")
        self.assertEqual(calls, ["setup", "build", "parse"])
        self.assertIs(parser.operation, operation)

    def test_cli_gemini_policy_configuration_is_visible_on_natural_language_outcome(self) -> None:
        class Parser:
            def parse(self, **kwargs):
                return RequestParseResult([], program_selection="ambiguous")

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            request_path = directory / "request.json"
            confirmation_path = directory / "confirmation.json"
            event_path = directory / "events.jsonl"
            usage_path = directory / "custom.sqlite3"
            request_path.write_text(
                json.dumps({"request_id": "cli-policy", "original_text": "find a flight"}),
                encoding="utf-8",
            )
            confirmation_path.write_text(json.dumps({"confirmed": False}), encoding="utf-8")
            with patch.object(
                sys,
                "argv",
                [
                    "app", "--request", str(request_path), "--confirmation", str(confirmation_path),
                    "--event-log", str(event_path), "--gemini-run-call-limit", "7",
                    "--gemini-daily-call-limit", "primary=11", "--gemini-daily-call-limit", "fallback=4",
                    "--gemini-usage-db-path", str(usage_path), "--gemini-timezone", "America/New_York",
                    "--gemini-max-attempts", "4", "--gemini-retry-backoff-seconds", "0.1,0.5",
                    "--gemini-operation-deadline-seconds", "12.5",
                ],
            ), patch("flight_search_demo.app.build_default_request_parser", return_value=Parser()):
                exit_code = main()
            event = json.loads(event_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        limits = event["gemini_usage"]["limits"]
        self.assertEqual(limits["run_call_limit"], 7)
        self.assertEqual(limits["daily_call_limits"]["primary"], 11)
        self.assertEqual(limits["daily_call_limits"]["fallback"], 4)
        self.assertEqual(limits["timezone_name"], "America/New_York")
        self.assertEqual(limits["day_boundary"], "midnight")
        self.assertEqual(limits["max_attempts"], 4)
        self.assertEqual(limits["retry_backoff_seconds"], [0.1, 0.5])
        self.assertEqual(limits["operation_deadline_seconds"], 12.5)

    def test_cli_setup_failure_still_reports_selected_gemini_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            request_path = directory / "request.json"
            confirmation_path = directory / "confirmation.json"
            event_path = directory / "events.jsonl"
            request_path.write_text(
                json.dumps({"request_id": "cli-setup-failure", "original_text": "find a flight"}),
                encoding="utf-8",
            )
            confirmation_path.write_text(json.dumps({"confirmed": False}), encoding="utf-8")
            with patch.object(
                sys,
                "argv",
                ["app", "--request", str(request_path), "--confirmation", str(confirmation_path),
                 "--event-log", str(event_path), "--gemini-run-call-limit", "6"],
            ), patch(
                "flight_search_demo.app.build_default_request_parser",
                side_effect=RuntimeError("client setup failed"),
            ):
                exit_code = main()
            event = json.loads(event_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(event["status"], "PARSER_FAILED")
        self.assertEqual(event["gemini_usage"]["limits"]["run_call_limit"], 6)

    def test_structured_cli_request_does_not_construct_default_parser(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            request_path = directory / "request.json"
            confirmation_path = directory / "confirmation.json"
            event_path = directory / "events.jsonl"
            request_path.write_text(
                json.dumps({
                    "request_id": "structured-zero-call",
                    "original_text": "structured request",
                    "program": "aeroplan", "origin": "JFK", "destination": "CDG",
                    "departure_date": "2026-11-05", "cabin": "Business", "adults": 1,
                    "trip_type": "one_way", "maximum_points": 70000,
                }),
                encoding="utf-8",
            )
            confirmation_path.write_text(json.dumps({"confirmed": False}), encoding="utf-8")
            with patch.object(
                sys,
                "argv",
                ["app", "--request", str(request_path), "--confirmation", str(confirmation_path),
                 "--event-log", str(event_path)],
            ), patch("flight_search_demo.app.build_default_request_parser") as builder, patch(
                "flight_search_demo.app.normalize_request",
                return_value=NormalizedCriteria(
                    "aeroplan", "JFK", "CDG", "2026-11-05", "Business", 1, "one_way", 70000
                ),
            ):
                main()
        builder.assert_not_called()

    def test_browser_operation_keeps_attempts_and_fallback_state_across_async_turns(self) -> None:
        calls: list[str] = []

        class RateLimitError(RuntimeError):
            status_code = 429

        async def provider(model: str):
            calls.append(model)
            if model == "primary":
                raise RateLimitError("rate limit")
            return f"ok-{len(calls)}"

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=3,
                    daily_call_limits={"primary": 2, "fallback": 2},
                    max_attempts=2,
                    retry_backoff_seconds=(),
                ),
            )
            operation = policy.operation("browser-multi-turn")
            first = asyncio.run(operation.invoke_async(
                model="primary", purpose="browser_agent", provider_call=provider,
                fallback_model="fallback",
            ))
            second = asyncio.run(operation.invoke_async(
                model="primary", purpose="browser_agent", provider_call=provider,
                fallback_model="fallback",
            ))
            diagnostics = operation.diagnostics()

        self.assertEqual(first, "ok-2")
        self.assertEqual(second, "ok-3")
        self.assertEqual(calls, ["primary", "fallback", "fallback"])
        self.assertEqual([item["attempt_number"] for item in diagnostics["call_records"]], [1, 2, 3])
        self.assertEqual(diagnostics["attempted_calls"], 3)
        self.assertEqual(diagnostics["fallback_calls"], 2)
        self.assertEqual(diagnostics["remaining_run_calls"], 0)


if __name__ == "__main__":
    unittest.main()
