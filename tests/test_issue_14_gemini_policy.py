from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from flight_search_demo.app import (
    ParsedNaturalLanguageRequest,
    GoogleGenAIRequestParser,
    RequestParseResult,
    run_request,
)
from flight_search_demo.live_run import PolicyBoundChatGoogle
from flight_search_demo.gemini_policy import (
    GeminiBudgetError,
    GeminiCallFailure,
    GeminiCallPolicy,
    GeminiErrorClassification,
    GeminiPolicyConfig,
    MalformedModelOutputError,
    classify_gemini_error,
    redact_sensitive_text,
)


class Issue14GeminiPolicyTests(unittest.TestCase):
    def test_natural_language_run_records_one_bounded_call_in_final_event(self) -> None:
        request = {
            "request_id": "req-policy-seam",
            "original_text": (
                "Find one Aeroplan business seat from JFK to CDG on 2026-11-05 "
                "under 70000 points"
            ),
        }
        parsed = ParsedNaturalLanguageRequest(
            "aeroplan", "JFK", "CDG", "2026-11-05", "Business", 1, "one_way", 70000
        )
        response = SimpleNamespace(
            outputs=[SimpleNamespace(type="text", text=json.dumps({
                "program_selection": "supported",
                "stated_program": "Aeroplan",
                "requests": [asdict(parsed)],
            }))]
        )
        client = SimpleNamespace(
            interactions=SimpleNamespace(create=lambda **_: response)
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            policy = GeminiCallPolicy(
                db_path=directory / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=1,
                    daily_call_limits={"gemini-test": 1},
                    timezone_name="UTC",
                ),
            )
            parser = GoogleGenAIRequestParser(
                client=client,
                model="gemini-test",
                policy=policy,
            )
            with redirect_stdout(io.StringIO()):
                events = run_request(
                    request=request,
                    confirmation={"confirmed": False},
                    event_log_path=directory / "events.jsonl",
                    parser=parser,
                    current_date=date(2026, 11, 1),
                    gemini_policy=policy,
                )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(events[0]["gemini_usage"]["attempted_calls"], 1)
        self.assertEqual(events[0]["gemini_usage"]["successful_calls"], 1)
        self.assertEqual(events[0]["gemini_usage"]["model"], "gemini-test")

    def test_structured_json_path_has_zero_gemini_calls_even_when_run_budget_is_zero(self) -> None:
        request = {
            "request_id": "req-structured-zero",
            "original_text": "Find one Aeroplan business seat from JFK to CDG on 2026-11-05 under 70000 points",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }
        parser = Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            policy = GeminiCallPolicy(
                db_path=directory / "usage.sqlite3",
                config=GeminiPolicyConfig(run_call_limit=0, daily_call_limits={"gemini-test": 1}),
            )
            events = run_request(
                request=request,
                confirmation={"confirmed": False},
                event_log_path=directory / "events.jsonl",
                parser=parser,
                current_date=date(2026, 11, 1),
                gemini_policy=policy,
            )

            parser.parse.assert_not_called()
            self.assertEqual(events[0]["status"], "CONFIRMATION_REQUIRED")
            self.assertEqual(policy.daily_usage("gemini-test"), 0)

    def test_run_allowance_counts_repeated_invocations_as_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(run_call_limit=2, daily_call_limits={"primary": 5}),
            )
            operation = policy.operation("op-max")
            calls = []

            def provider(model: str) -> str:
                calls.append(model)
                return "ok"

            self.assertEqual(operation.invoke(model="primary", purpose="request_parse", provider_call=provider), "ok")
            self.assertEqual(operation.invoke(model="primary", purpose="request_parse", provider_call=provider), "ok")
            with self.assertRaisesRegex(Exception, "per-run"):
                operation.invoke(model="primary", purpose="request_parse", provider_call=provider)

            self.assertEqual(calls, ["primary", "primary"])
            self.assertEqual(operation.diagnostics()["attempted_calls"], 2)

    def test_daily_counter_survives_restart_and_rolls_at_configured_timezone_boundary(self) -> None:
        current = [datetime(2026, 1, 2, 4, 59, tzinfo=timezone.utc)]

        def now() -> datetime:
            return current[0]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "usage.sqlite3"
            config = GeminiPolicyConfig(
                run_call_limit=3,
                daily_call_limits={"primary": 1},
                timezone_name="America/New_York",
            )
            first = GeminiCallPolicy(db_path=db_path, config=config, clock=now)
            first.operation("first").invoke(
                model="primary", purpose="request_parse", provider_call=lambda _: "ok"
            )
            restarted = GeminiCallPolicy(db_path=db_path, config=config, clock=now)
            with self.assertRaises(GeminiBudgetError) as exhausted:
                restarted.operation("blocked").invoke(
                    model="primary", purpose="request_parse", provider_call=lambda _: "no"
                )
            self.assertEqual(
                exhausted.exception.classification,
                GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
            )

            current[0] = datetime(2026, 1, 2, 5, 0, tzinfo=timezone.utc)
            next_day = GeminiCallPolicy(db_path=db_path, config=config, clock=now)
            next_day.operation("next-day").invoke(
                model="primary", purpose="request_parse", provider_call=lambda _: "ok"
            )
            self.assertEqual(next_day.usage_day(), "2026-01-02")

    def test_transient_retry_is_bounded_and_backoff_is_observable(self) -> None:
        sleeps = []
        calls = []

        class TransientError(RuntimeError):
            status_code = 503

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=3,
                    daily_call_limits={"primary": 3},
                    max_attempts=3,
                    retry_backoff_seconds=(0.25, 0.5),
                ),
                sleep=sleeps.append,
            )
            operation = policy.operation("retry")

            def provider(model: str) -> str:
                calls.append(model)
                if len(calls) < 3:
                    raise TransientError("service unavailable")
                return "ok"

            self.assertEqual(
                operation.invoke(model="primary", purpose="browser_agent", provider_call=provider),
                "ok",
            )
            self.assertEqual(calls, ["primary", "primary", "primary"])
            self.assertEqual(sleeps, [0.25, 0.5])
            self.assertEqual(operation.diagnostics()["retried_calls"], 2)

    def test_retry_backoff_cannot_outlive_operation_deadline(self) -> None:
        monotonic_values = iter((0.0, 0.0, 0.0))
        calls = []

        class TransientError(RuntimeError):
            status_code = 503

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=3,
                    daily_call_limits={"primary": 3},
                    max_attempts=3,
                    retry_backoff_seconds=(1.0,),
                ),
                monotonic=lambda: next(monotonic_values),
                sleep=lambda _: self.fail("deadline-bounded retry must not sleep"),
            )
            operation = policy.operation("deadline", deadline_seconds=0.5)
            with self.assertRaises(GeminiCallFailure) as failure:
                operation.invoke(
                    model="primary",
                    purpose="request_parse",
                    provider_call=lambda model: (calls.append(model), (_ for _ in ()).throw(TransientError("503")))[1],
                )

        self.assertEqual(calls, ["primary"])
        self.assertEqual(
            failure.exception.classification,
            GeminiErrorClassification.TRANSIENT_PROVIDER_ERROR,
        )
        self.assertEqual(operation.records[0].retry_decision, "deadline_exhausted")

    def test_rate_limit_fallback_uses_remaining_run_budget_and_different_model(self) -> None:
        calls = []

        class RateLimitError(RuntimeError):
            status_code = 429

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=2,
                    daily_call_limits={"primary": 1, "fallback": 1},
                    max_attempts=2,
                ),
            )
            operation = policy.operation("fallback")

            def provider(model: str) -> str:
                calls.append(model)
                if model == "primary":
                    raise RateLimitError("RPM rate limit")
                return "fallback-ok"

            self.assertEqual(
                operation.invoke(
                    model="primary",
                    purpose="browser_agent",
                    provider_call=provider,
                    fallback_model="fallback",
                ),
                "fallback-ok",
            )
            self.assertEqual(calls, ["primary", "fallback"])
            self.assertEqual(operation.diagnostics()["fallback_calls"], 1)
            self.assertEqual(operation.diagnostics()["remaining_run_calls"], 0)

    def test_fallback_daily_exhaustion_stops_without_calling_fallback(self) -> None:
        calls = []

        class RateLimitError(RuntimeError):
            status_code = 429

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=2,
                    daily_call_limits={"primary": 1, "fallback": 0},
                    max_attempts=2,
                ),
            )
            operation = policy.operation("fallback-blocked")
            with self.assertRaises(GeminiBudgetError) as blocked:
                operation.invoke(
                    model="primary",
                    purpose="browser_agent",
                    provider_call=lambda model: (calls.append(model), (_ for _ in ()).throw(RateLimitError("429")))[1],
                    fallback_model="fallback",
                )
            self.assertEqual(blocked.exception.classification, GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION)
            self.assertEqual(calls, ["primary"])

    def test_required_provider_classifications_and_redaction_are_stable(self) -> None:
        class ErrorWithCode(RuntimeError):
            def __init__(self, message: str, status_code: int | None = None) -> None:
                super().__init__(message)
                self.status_code = status_code

        cases = (
            (ErrorWithCode("RPM rate limit", 429), GeminiErrorClassification.RPM_RATE_LIMIT),
            (ErrorWithCode("daily quota exhausted", 429), GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION),
            (ErrorWithCode("service unavailable", 503), GeminiErrorClassification.TRANSIENT_PROVIDER_ERROR),
            (ErrorWithCode("invalid API key", 401), GeminiErrorClassification.AUTH_INVALID_ERROR),
            (TimeoutError("request timed out"), GeminiErrorClassification.TIMEOUT_CANCELLATION),
            (MalformedModelOutputError("invalid JSON"), GeminiErrorClassification.MALFORMED_OUTPUT),
            (ErrorWithCode("unexpected provider response", 418), GeminiErrorClassification.UNKNOWN_PROVIDER_ERROR),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_gemini_error(error), expected)

        safe = redact_sensitive_text(
            "api_key=AIzaSyA123456789012345678901 https://example.test/session?id=secret "
            "Authorization: Bearer abc123 cookie=secret"
        )
        self.assertNotIn("AIzaSyA123456789012345678901", safe)
        self.assertNotIn("id=secret", safe)
        self.assertNotIn("Bearer abc123", safe)
        self.assertNotIn("cookie=secret", safe)

    def test_daily_exhaustion_is_an_explicit_application_outcome_without_a_provider_call(self) -> None:
        request = {
            "request_id": "req-quota",
            "original_text": "Find one Aeroplan business seat from JFK to CDG on 2026-11-05 under 70000 points",
        }
        calls = []
        client = SimpleNamespace(
            interactions=SimpleNamespace(create=lambda **_: calls.append("called"))
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            policy = GeminiCallPolicy(
                db_path=directory / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=2,
                    daily_call_limits={"gemini-test": 0},
                ),
            )
            parser = GoogleGenAIRequestParser(client=client, model="gemini-test", policy=policy)
            events = run_request(
                request=request,
                confirmation={"confirmed": True},
                event_log_path=directory / "events.jsonl",
                parser=parser,
                current_date=date(2026, 11, 1),
                gemini_policy=policy,
            )

        self.assertEqual(events[0]["status"], "GEMINI_QUOTA_EXHAUSTED")
        self.assertEqual(calls, [])
        self.assertEqual(events[0]["gemini_usage"]["attempted_calls"], 0)
        self.assertEqual(
            events[0]["gemini_usage"]["terminal_classification"],
            "daily_quota_exhaustion",
        )

    def test_browser_agent_model_boundary_uses_same_budget_and_fallback(self) -> None:
        class RateLimitError(RuntimeError):
            status_code = 429

        class FakeLLM:
            def __init__(self, model: str) -> None:
                self.model = model
                self.calls = []

            async def ainvoke(self, messages, output_format=None, **kwargs):
                self.calls.append(self.model)
                if self.model == "primary":
                    raise RateLimitError("RPM rate limit")
                return "browser-result"

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=2,
                    daily_call_limits={"primary": 1, "fallback": 1},
                    max_attempts=2,
                ),
            )
            primary = FakeLLM("primary")
            fallback = FakeLLM("fallback")
            operation = policy.operation("browser")
            llm = PolicyBoundChatGoogle(
                primary=primary,
                fallback=fallback,
                operation=operation,
            )
            result = asyncio.run(llm.ainvoke([], None))
            self.assertEqual(result, "browser-result")
            self.assertEqual(primary.calls, ["primary"])
            self.assertEqual(fallback.calls, ["fallback"])
            self.assertEqual(operation.diagnostics()["attempted_calls"], 2)


if __name__ == "__main__":
    unittest.main()
