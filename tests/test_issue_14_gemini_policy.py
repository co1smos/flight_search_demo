from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from flight_search_demo.app import (
    ParsedNaturalLanguageRequest,
    GoogleGenAIRequestParser,
    ParserFailure,
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
    redact_sensitive,
    redact_sensitive_text,
)


class Issue14GeminiPolicyTests(unittest.TestCase):
    def test_limits_summary_exposes_selected_budget_retry_and_deadline_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=4,
                    daily_call_limits={"primary": 7},
                    timezone_name="UTC",
                    max_attempts=3,
                    retry_backoff_seconds=(0.2, 0.5),
                    operation_deadline_seconds=12.5,
                ),
            )
            summary = policy.limits_summary()

        self.assertEqual(summary["run_call_limit"], 4)
        self.assertEqual(summary["daily_call_limits"], {"primary": 7})
        self.assertEqual(summary["timezone_name"], "UTC")
        self.assertEqual(summary["max_attempts"], 3)
        self.assertEqual(summary["retry_backoff_seconds"], [0.2, 0.5])
        self.assertEqual(summary["operation_deadline_seconds"], 12.5)

    def test_google_parser_requires_a_caller_owned_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "caller-owned Gemini policy"):
            GoogleGenAIRequestParser(client=SimpleNamespace(), model="gemini-test", policy=None)

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

    def test_natural_language_provider_receives_the_remaining_sdk_timeout(self) -> None:
        request = {
            "request_id": "req-sdk-timeout",
            "original_text": "Find one Aeroplan business seat from JFK to CDG on 2026-11-05 under 70000 points",
        }
        parsed = ParsedNaturalLanguageRequest(
            "aeroplan", "JFK", "CDG", "2026-11-05", "Business", 1, "one_way", 70000
        )
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                outputs=[SimpleNamespace(type="text", text=json.dumps({
                    "program_selection": "supported",
                    "stated_program": "Aeroplan",
                    "requests": [asdict(parsed)],
                }))]
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=1,
                    daily_call_limits={"gemini-test": 1},
                    operation_deadline_seconds=0.75,
                ),
            )
            parser = GoogleGenAIRequestParser(
                client=SimpleNamespace(interactions=SimpleNamespace(create=create)),
                model="gemini-test",
                policy=policy,
            )
            result = parser.parse(
                request_id=request["request_id"],
                original_text=request["original_text"],
                current_date=date(2026, 11, 1),
                timezone_name="UTC",
            )

        self.assertEqual(len(result.requests), 1)
        self.assertEqual(len(calls), 1)
        self.assertGreater(calls[0]["timeout"], 0)
        self.assertLessEqual(calls[0]["timeout"], 0.75)

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

    def test_run_allowance_is_shared_by_multiple_operations_and_concurrent_reservations(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=2,
                    daily_call_limits={"primary": 10},
                ),
            )
            operations = [policy.operation(f"op-{index}") for index in range(4)]

            def provider(model: str) -> str:
                calls.append(model)
                time.sleep(0.03)
                return "ok"

            def invoke(operation):
                try:
                    return operation.invoke(
                        model="primary", purpose="browser_agent", provider_call=provider
                    )
                except GeminiBudgetError as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(invoke, operations))

        self.assertEqual(sum(result == "ok" for result in results), 2)
        self.assertEqual(sum(isinstance(result, GeminiBudgetError) for result in results), 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sum(operation.attempted_calls for operation in operations), 2)

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
                monotonic=lambda: 0.0,
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

    def test_sync_provider_call_is_bounded_by_the_original_operation_deadline(self) -> None:
        calls = []
        completed = []
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(run_call_limit=1, daily_call_limits={"primary": 1}),
            )
            operation = policy.operation("sync-timeout", deadline_seconds=0.05)

            def provider(model: str) -> str:
                calls.append(model)
                time.sleep(0.08)
                completed.append(True)
                return "late success"

            started = time.monotonic()
            with self.assertRaises(GeminiCallFailure) as failure:
                operation.invoke(model="primary", purpose="request_parse", provider_call=provider)
            elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.07)
        self.assertEqual(calls, ["primary"])
        self.assertEqual(completed, [True])
        self.assertEqual(
            failure.exception.classification,
            GeminiErrorClassification.TIMEOUT_CANCELLATION,
        )
        self.assertEqual(operation.records[0].classification, "timeout_cancellation")

    def test_deadline_after_reservation_releases_run_allowance_exactly_once(self) -> None:
        monotonic_values = iter((0.0, 0.0, 0.0, 2.0))
        calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=1,
                    daily_call_limits={"primary": 1},
                    operation_deadline_seconds=1.0,
                ),
                monotonic=lambda: next(monotonic_values),
            )
            operation = policy.operation("post-reservation-timeout")

            with self.assertRaises(GeminiCallFailure) as failure:
                operation.invoke(
                    model="primary",
                    purpose="request_parse",
                    provider_call=lambda model: calls.append(model),
                )

            self.assertEqual(
                failure.exception.classification,
                GeminiErrorClassification.TIMEOUT_CANCELLATION,
            )
            self.assertEqual(calls, [])
            self.assertEqual(operation.diagnostics()["remaining_run_calls"], 1)
            self.assertEqual(policy.daily_usage("primary"), 0)

    def test_sync_reservation_contention_is_bounded_by_operation_deadline(self) -> None:
        calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "usage.sqlite3"
            policy = GeminiCallPolicy(
                db_path=db_path,
                config=GeminiPolicyConfig(
                    run_call_limit=1,
                    daily_call_limits={"primary": 1},
                ),
            )
            lock = sqlite3.connect(db_path)
            lock.execute("BEGIN IMMEDIATE")
            operation = policy.operation("sync-sqlite-timeout", deadline_seconds=0.05)
            started = time.monotonic()
            try:
                with self.assertRaises(GeminiCallFailure) as failure:
                    operation.invoke(
                        model="primary",
                        purpose="request_parse",
                        provider_call=lambda model: calls.append(model),
                    )
            finally:
                lock.rollback()
                lock.close()
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.5)
            self.assertEqual(
                failure.exception.classification,
                GeminiErrorClassification.TIMEOUT_CANCELLATION,
            )
            self.assertEqual(calls, [])
            self.assertEqual(operation.diagnostics()["remaining_run_calls"], 1)
            self.assertEqual(policy.daily_usage("primary"), 0)

    def test_async_provider_call_is_cancellable_at_the_original_operation_deadline(self) -> None:
        async def exercise():
            with tempfile.TemporaryDirectory() as tmpdir:
                policy = GeminiCallPolicy(
                    db_path=Path(tmpdir) / "usage.sqlite3",
                    config=GeminiPolicyConfig(run_call_limit=1, daily_call_limits={"primary": 1}),
                )
                operation = policy.operation("async-timeout", deadline_seconds=0.05)

                async def provider(model: str) -> str:
                    await asyncio.sleep(0.25)
                    return "late success"

                started = time.monotonic()
                with self.assertRaises(GeminiCallFailure) as failure:
                    await operation.invoke_async(
                        model="primary", purpose="browser_agent", provider_call=provider
                    )
                return time.monotonic() - started, failure.exception, operation

        elapsed, failure, operation = asyncio.run(exercise())
        self.assertLess(elapsed, 0.18)
        self.assertEqual(failure.classification, GeminiErrorClassification.TIMEOUT_CANCELLATION)
        self.assertEqual(operation.records[0].classification, "timeout_cancellation")

    def test_async_caller_cancellation_is_reraised_unchanged(self) -> None:
        async def exercise():
            with tempfile.TemporaryDirectory() as tmpdir:
                policy = GeminiCallPolicy(
                    db_path=Path(tmpdir) / "usage.sqlite3",
                    config=GeminiPolicyConfig(run_call_limit=1, daily_call_limits={"primary": 1}),
                )
                operation = policy.operation("caller-cancel", deadline_seconds=1)
                started = asyncio.Event()

                async def provider(model: str) -> str:
                    started.set()
                    await asyncio.Event().wait()
                    return "never"

                task = asyncio.create_task(
                    operation.invoke_async(
                        model="primary", purpose="browser_agent", provider_call=provider
                    )
                )
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                return operation

        operation = asyncio.run(exercise())
        self.assertEqual(operation.records[0].classification, "timeout_cancellation")

    def test_async_reservation_and_diagnostic_reads_do_not_wait_for_sqlite_busy_timeout(self) -> None:
        async def exercise():
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "usage.sqlite3"
                policy = GeminiCallPolicy(
                    db_path=db_path,
                    config=GeminiPolicyConfig(run_call_limit=1, daily_call_limits={"primary": 1}),
                )
                lock = sqlite3.connect(db_path)
                lock.execute("BEGIN IMMEDIATE")
                operation = policy.operation("sqlite-timeout", deadline_seconds=0.05)
                ticks = 0

                async def ticker():
                    nonlocal ticks
                    while True:
                        ticks += 1
                        await asyncio.sleep(0.005)

                tick_task = asyncio.create_task(ticker())
                try:
                    with self.assertRaises(GeminiCallFailure) as failure:
                        await operation.invoke_async(
                            model="primary",
                            purpose="browser_agent",
                            provider_call=lambda _: asyncio.sleep(0),
                        )
                    diagnostic_started = time.monotonic()
                    await operation.diagnostics_async(deadline_seconds=0.05)
                    diagnostic_elapsed = time.monotonic() - diagnostic_started
                finally:
                    lock.rollback()
                    lock.close()
                    tick_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await tick_task
                return ticks, failure.exception, diagnostic_elapsed

        ticks, failure, diagnostic_elapsed = asyncio.run(exercise())
        self.assertGreater(ticks, 2)
        self.assertEqual(failure.classification, GeminiErrorClassification.TIMEOUT_CANCELLATION)
        self.assertLess(diagnostic_elapsed, 0.15)

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

    def test_transient_retry_reserves_a_fallback_attempt(self) -> None:
        calls = []

        class TransientError(RuntimeError):
            status_code = 503

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=3,
                    daily_call_limits={"primary": 2, "fallback": 1},
                    max_attempts=3,
                    retry_backoff_seconds=(),
                ),
            )
            operation = policy.operation("retry-then-fallback")

            def provider(model: str) -> str:
                calls.append(model)
                if model == "primary":
                    raise TransientError("service unavailable")
                return "fallback-ok"

            result = operation.invoke(
                model="primary",
                purpose="browser_agent",
                provider_call=provider,
                fallback_model="fallback",
            )
            self.assertEqual(operation.diagnostics()["attempted_calls"], 3)

        self.assertEqual(result, "fallback-ok")
        self.assertEqual(calls, ["primary", "primary", "fallback"])

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
            diagnostics = blocked.exception.diagnostics
            self.assertEqual(diagnostics["model"], "fallback")
            self.assertEqual(diagnostics["fallback_decision"], "blocked_daily_budget")
            self.assertEqual(diagnostics["attempted_calls"], 1)
            self.assertEqual(diagnostics["reservation_denial"]["allowance"], "daily_call_limit")
            self.assertEqual(diagnostics["reservation_denial"]["remaining_allowance"], 0)
            self.assertFalse(diagnostics["reservation_denial"]["provider_call_started"])

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

        headers = redact_sensitive_text(
            "Cookie: sid=one; csrf=two\n"
            "Set-Cookie: session=three; Path=/; HttpOnly; SameSite=Strict"
        )
        for secret in ("sid=one", "csrf=two", "session=three", "Path=/", "HttpOnly", "SameSite=Strict"):
            self.assertNotIn(secret, headers)

    def test_diagnostic_redaction_covers_authorization_schemes_proxy_headers_and_urls(self) -> None:
        sensitive = (
            "Authorization: Basic dXNlcjpzZWNyZXQ=\n"
            "Authorization: Digest username=alice, nonce=nonce-secret, response=response-secret\n"
            "Authorization: Custom opaque-authorization-secret\n"
            "Proxy-Authorization: Digest username=proxy, response=proxy-response-secret\n"
            "Cookie: sid=session-secret; csrf=csrf-secret\n"
            "credentials=user:password-secret api_key=api-secret\n"
            "https://user:url-password-secret@sensitive.test/account/session-secret?session=session-url-secret&token=url-token-secret"
        )

        safe = redact_sensitive_text(sensitive)

        for secret in (
            "dXNlcjpzZWNyZXQ=",
            "nonce-secret",
            "response-secret",
            "opaque-authorization-secret",
            "proxy-response-secret",
            "session-secret",
            "csrf-secret",
            "password-secret",
            "api-secret",
            "url-password-secret",
            "session-url-secret",
            "url-token-secret",
        ):
            self.assertNotIn(secret, safe)
        self.assertIn("Authorization", safe)
        self.assertIn("Proxy-Authorization", safe)
        self.assertIn("sensitive.test", safe)

        structured = redact_sensitive({
            "headers": [
                {"name": "Authorization", "value": "Basic header-secret"},
                {"name": "X-Request-ID", "value": "keep-this-id"},
            ],
            "websocket": "wss://127.0.0.1/devtools/browser/session-secret",
        })
        self.assertEqual(structured["headers"][0]["name"], "Authorization")
        self.assertEqual(structured["headers"][0]["value"], "[REDACTED]")
        self.assertEqual(structured["headers"][1]["value"], "keep-this-id")
        self.assertNotIn("session-secret", structured["websocket"])

    def test_diagnostic_jsonl_redacts_original_text_and_sensitive_mapping_keys(self) -> None:
        class FailingParser:
            def parse(self, **kwargs):
                raise ParserFailure(
                    "provider rejected password=opaque-secret",
                    diagnostics={
                        "ordinary": "keep-this-diagnostic",
                        "password": "opaque-secret",
                        "nested": {
                            "api_key": "ordinary-api-value",
                            "session": {"token": "session-value"},
                        },
                        "sensitive_url": "https://sensitive.test/session?token=url-secret",
                    },
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            run_request(
                request={
                    "request_id": "req-redaction",
                    "original_text": "search https://sensitive.test/session?token=original-secret",
                },
                confirmation={"confirmed": False},
                event_log_path=directory / "events.jsonl",
                parser=FailingParser(),
            )
            diagnostic_text = (directory / "events.diagnostics.jsonl").read_text(encoding="utf-8")

        for secret in (
            "original-secret",
            "opaque-secret",
            "ordinary-api-value",
            "session-value",
            "url-secret",
        ):
            self.assertNotIn(secret, diagnostic_text)
        self.assertIn("keep-this-diagnostic", diagnostic_text)

    def test_persisted_diagnostic_redacts_whitespace_credentials_and_standalone_auth_schemes(self) -> None:
        class FailingParser:
            def parse(self, **kwargs):
                raise ParserFailure(
                    "provider error: password hunter2; credentials are user@example.com / p@ssw0rd; "
                    "cookie sid=abc; csrf=def; Bearer opaque-secret-token; "
                    "Basic dXNlcjpzZWNyZXQ=; Digest username=alice, nonce=nonce-secret, response=response-secret; "
                    "authorization custom-auth-secret; api key api-whitespace-secret; "
                    "session material session-material-secret",
                    diagnostics={"provenance": "provider-call", "detail": "keep-this-provenance"},
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            run_request(
                request={
                    "request_id": "req-broad-redaction",
                    "original_text": "find an award",
                },
                confirmation={"confirmed": False},
                event_log_path=directory / "events.jsonl",
                parser=FailingParser(),
            )
            diagnostic_text = (directory / "events.diagnostics.jsonl").read_text(encoding="utf-8")

        for secret in (
            "hunter2",
            "user@example.com",
            "p@ssw0rd",
            "sid=abc",
            "csrf=def",
            "opaque-secret-token",
            "dXNlcjpzZWNyZXQ=",
            "nonce-secret",
            "response-secret",
            "custom-auth-secret",
            "api-whitespace-secret",
            "session-material-secret",
        ):
            self.assertNotIn(secret, diagnostic_text)
        self.assertIn("keep-this-provenance", diagnostic_text)

    def test_final_outcome_identifies_fallback_reservation_denial(self) -> None:
        class RateLimitError(RuntimeError):
            status_code = 429

        calls = []

        def create(**kwargs):
            calls.append(kwargs["model"])
            raise RateLimitError("RPM rate limit")

        client = SimpleNamespace(interactions=SimpleNamespace(create=create))
        request = {
            "request_id": "req-fallback-denial-seam",
            "original_text": (
                "Find one Aeroplan business seat from JFK to CDG on 2026-11-05 "
                "under 70000 points"
            ),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            policy = GeminiCallPolicy(
                db_path=directory / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=2,
                    daily_call_limits={"primary": 1, "fallback": 0},
                    max_attempts=2,
                ),
            )
            parser = GoogleGenAIRequestParser(
                client=client,
                model="primary",
                policy=policy,
                fallback_model="fallback",
            )
            with redirect_stdout(io.StringIO()):
                events = run_request(
                    request=request,
                    confirmation={"confirmed": True},
                    event_log_path=directory / "events.jsonl",
                    parser=parser,
                    current_date=date(2026, 11, 1),
                    gemini_policy=policy,
                )

        self.assertEqual(calls, ["primary"])
        self.assertEqual(events[0]["status"], "GEMINI_QUOTA_EXHAUSTED")
        usage = events[0]["gemini_usage"]
        self.assertEqual(usage["attempted_calls"], 1)
        self.assertEqual(usage["model"], "fallback")
        self.assertEqual(usage["purpose"], "request_parse")
        self.assertEqual(usage["fallback_decision"], "blocked_daily_budget")
        self.assertEqual(usage["terminal_classification"], "daily_quota_exhaustion")
        denial = usage["reservation_denial"]
        self.assertEqual(denial["model"], "fallback")
        self.assertEqual(denial["purpose"], "request_parse")
        self.assertEqual(denial["decision"], "blocked_daily_budget")
        self.assertEqual(denial["allowance"], "daily_call_limit")
        self.assertEqual(denial["allowance_limit"], 0)
        self.assertEqual(denial["remaining_allowance"], 0)
        self.assertEqual(denial["classification"], "daily_quota_exhaustion")
        self.assertTrue(denial["timestamp"])

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

    def test_browser_run_budget_exhaustion_is_a_structured_application_outcome(self) -> None:
        from flight_search_demo.live_run import run_spike

        calls = []
        with tempfile.TemporaryDirectory() as tmpdir:
            args = SimpleNamespace(
                steel_base_url="http://127.0.0.1:3000",
                controlled_page_public_origin="http://127.0.0.1:8765",
                controlled_page_port=8765,
                start_local_controlled_page_server=False,
                storage_state_path=str(Path(tmpdir) / "state.json"),
                handoff_file=str(Path(tmpdir) / "handoff.json"),
                marker="marker",
                gemini_model="primary",
                fallback_gemini_model="fallback",
                max_steps=2,
                handoff_timeout=1,
                gemini_run_call_limit=0,
                gemini_usage_db_path=str(Path(tmpdir) / "usage.sqlite3"),
            )
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=0,
                    daily_call_limits={"primary": 1},
                ),
            )

            def no_provider_call(*args, **kwargs):
                calls.append((args, kwargs))
                raise AssertionError("budget exhaustion must stop before a Gemini provider call")

            with patch.dict(os.environ, {"GOOGLE_API_KEY": "offline-test"}, clear=True), patch(
                "flight_search_demo.live_run.run_in_fresh_session", new=AsyncMock(
                    side_effect=GeminiBudgetError(
                        "per-run Gemini call allowance exhausted",
                        classification=GeminiErrorClassification.RUN_BUDGET_EXHAUSTION,
                        diagnostics=policy.operation("browser").diagnostics(
                            terminal_classification=GeminiErrorClassification.RUN_BUDGET_EXHAUSTION,
                            terminal_error="per-run Gemini call allowance exhausted",
                            include_daily_reads=False,
                        ),
                    )
                )
            ), patch("flight_search_demo.live_run.run_agent_task", new=AsyncMock(
                side_effect=no_provider_call
            )) as agent_task:
                summary = asyncio.run(run_spike(args))

        self.assertEqual(summary.status, "GEMINI_BUDGET_EXHAUSTED")
        self.assertIsNone(summary.initial_result)
        self.assertEqual(summary.gemini_outcome["status"], "GEMINI_BUDGET_EXHAUSTED")
        self.assertTrue(summary.gemini_outcome["diagnostic_id"])
        self.assertEqual(summary.gemini_outcome["remaining_run_calls"], 0)
        self.assertEqual(calls, [])
        agent_task.assert_not_awaited()

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
