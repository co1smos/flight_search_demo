from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date as calendar_date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Mapping
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flight_search_demo.app import (
    AeroplanFixtureAdapter,
    AwardProviderAdapter,
    DEFAULT_ADAPTER_REGISTRY,
    GoogleGenAIRequestParser,
    NormalizedCriteria,
    ParsedNaturalLanguageRequest,
    ParserFailure,
    RequestParseResult,
    RequestParser,
    build_confirmation_request,
    build_request_hash,
    normalize_airport,
    normalize_departure_date,
    normalize_request,
    main,
    run_request,
    run_structured_request,
)


class Issue3ApplicationTests(unittest.TestCase):
    def test_natural_language_request_requires_confirmation_with_normalized_criteria(self) -> None:
        class FakeParser(RequestParser):
            def parse(
                self,
                *,
                request_id: str,
                original_text: str,
                current_date: calendar_date,
                timezone_name: str,
            ) -> RequestParseResult:
                self.call = (request_id, original_text, current_date, timezone_name)
                return RequestParseResult(
                    requests=[
                        ParsedNaturalLanguageRequest(
                            program="aeroplan",
                            origin="JFK",
                            destination="CDG",
                            departure_date="2026-11-05",
                            cabin="Business",
                            adults=1,
                            trip_type="one_way",
                            maximum_points=70000,
                        )
                    ],
                    diagnostics={"parser": "fake"},
                )

        parser = FakeParser()
        request = {
            "request_id": "req-nl-confirm",
            "original_text": "Find one business Aeroplan seat from JFK to CDG next Thursday under 70k",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_request(
                request=request,
                confirmation={"confirmed": False},
                event_log_path=Path(tmpdir) / "events.jsonl",
                parser=parser,
                current_date=calendar_date(2026, 11, 1),
                timezone_name="America/New_York",
            )

        self.assertEqual(
            parser.call,
            (
                "req-nl-confirm",
                request["original_text"],
                calendar_date(2026, 11, 1),
                "America/New_York",
            ),
        )
        self.assertEqual(len(result), 1)
        event = result[0]
        self.assertEqual(event["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(event["normalized_criteria"]["program"], "aeroplan")
        self.assertEqual(event["normalized_criteria"]["departure_date"], "2026-11-05")
        self.assertEqual(event["detail"], "explicit confirmation is required")

    def test_natural_language_ambiguity_requires_clarification_before_execution(self) -> None:
        class AmbiguousParser(RequestParser):
            def parse(
                self,
                *,
                request_id: str,
                original_text: str,
                current_date: calendar_date,
                timezone_name: str,
            ) -> RequestParseResult:
                return RequestParseResult(
                    requests=[],
                    clarification="'ANA flight' is ambiguous; confirm ANA Mileage Club or a flight operated by ANA.",
                    diagnostics={"parser": "fake"},
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_request(
                request={
                    "request_id": "req-nl-ambiguous",
                    "original_text": "Find an ANA flight from JFK to CDG next Thursday in business",
                },
                confirmation={"confirmed": True},
                event_log_path=Path(tmpdir) / "events.jsonl",
                parser=AmbiguousParser(),
                current_date=calendar_date(2026, 11, 1),
                timezone_name="America/New_York",
            )

        self.assertEqual(len(result), 1)
        event = result[0]
        self.assertEqual(event["status"], "CLARIFICATION_REQUIRED")
        self.assertIn("ambiguous", event["detail"])
        self.assertIsNone(event["atomic_task_id"])

    def test_confirmed_multi_program_request_creates_one_atomic_task_per_program(self) -> None:
        class MultiProgramParser(RequestParser):
            def parse(
                self,
                *,
                request_id: str,
                original_text: str,
                current_date: calendar_date,
                timezone_name: str,
            ) -> RequestParseResult:
                return RequestParseResult(
                    requests=[
                        ParsedNaturalLanguageRequest(
                            program="aeroplan",
                            origin="JFK",
                            destination="CDG",
                            departure_date="2026-11-05",
                            cabin="Business",
                            adults=1,
                            trip_type="one_way",
                            maximum_points=70000,
                        ),
                        ParsedNaturalLanguageRequest(
                            program="ana",
                            origin="JFK",
                            destination="CDG",
                            departure_date="2026-11-05",
                            cabin="Business",
                            adults=1,
                            trip_type="one_way",
                            maximum_points=70000,
                        ),
                    ],
                    diagnostics={"parser": "fake"},
                )

        class RecordingAdapter(AwardProviderAdapter):
            def __init__(self, status: str) -> None:
                self.status = status
                self.calls = []

            def execute(
                self, criteria: NormalizedCriteria, atomic_task_id: str
            ) -> Dict[str, str]:
                self.calls.append((criteria, atomic_task_id))
                return {"status": self.status, "detail": f"{criteria.program} ran"}

        aeroplan = RecordingAdapter("MATCH_FOUND")
        ana = RecordingAdapter("NO_AWARD_AVAILABILITY")
        adapter_registry = {"aeroplan": aeroplan, "ana": ana}
        request = {
            "request_id": "req-nl-multi",
            "original_text": "Monitor Aeroplan and ANA from JFK to CDG on November 5 2026 in business under 70000 points",
        }
        confirmation = build_confirmation_request(
            request=request,
            parsed_requests=[
                ParsedNaturalLanguageRequest(
                    program="aeroplan",
                    origin="JFK",
                    destination="CDG",
                    departure_date="2026-11-05",
                    cabin="Business",
                    adults=1,
                    trip_type="one_way",
                    maximum_points=70000,
                ),
                ParsedNaturalLanguageRequest(
                    program="ana",
                    origin="JFK",
                    destination="CDG",
                    departure_date="2026-11-05",
                    cabin="Business",
                    adults=1,
                    trip_type="one_way",
                    maximum_points=70000,
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_request(
                request=request,
                confirmation=confirmation,
                event_log_path=Path(tmpdir) / "events.jsonl",
                parser=MultiProgramParser(),
                adapter_registry=adapter_registry,
                current_date=calendar_date(2026, 11, 1),
                timezone_name="America/New_York",
            )

        self.assertEqual([event["program"] for event in result], ["aeroplan", "ana"])
        self.assertEqual(len(aeroplan.calls), 1)
        self.assertEqual(len(ana.calls), 1)
        self.assertNotEqual(result[0]["atomic_task_id"], result[1]["atomic_task_id"])
        self.assertEqual(aeroplan.calls[0][0].maximum_points, 70000)
        self.assertEqual(ana.calls[0][0].maximum_points, 70000)

    def test_parser_failure_is_reported_and_does_not_authorize_execution(self) -> None:
        class FailingParser(RequestParser):
            def parse(
                self,
                *,
                request_id: str,
                original_text: str,
                current_date: calendar_date,
                timezone_name: str,
            ) -> RequestParseResult:
                raise ParserFailure("sdk parse failed", diagnostics={"parser": "fake"})

        class RecordingAdapter(AwardProviderAdapter):
            def execute(
                self, criteria: NormalizedCriteria, atomic_task_id: str
            ) -> Dict[str, str]:
                raise AssertionError("execution should not occur after parser failure")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_request(
                request={
                    "request_id": "req-parse-fail",
                    "original_text": "Search for something vague",
                },
                confirmation={"confirmed": True},
                event_log_path=Path(tmpdir) / "events.jsonl",
                parser=FailingParser(),
                adapter_registry={"aeroplan": RecordingAdapter()},
                current_date=calendar_date(2026, 11, 1),
            )

            diagnostic = json.loads((Path(tmpdir) / "events.diagnostics.jsonl").read_text())

        self.assertEqual(result[0]["status"], "PARSER_FAILED")
        self.assertEqual(result[0]["diagnostic_id"], diagnostic["diagnostic_id"])
        self.assertEqual(diagnostic["metadata"]["parser"], "fake")

    def test_empty_parse_result_is_reported_as_parser_failure(self) -> None:
        class EmptyParser(RequestParser):
            def parse(
                self,
                *,
                request_id: str,
                original_text: str,
                current_date: calendar_date,
                timezone_name: str,
            ) -> RequestParseResult:
                return RequestParseResult(requests=[], diagnostics={"parser": "fake"})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_request(
                request={
                    "request_id": "req-empty-parse",
                    "original_text": "Find me something",
                },
                confirmation={"confirmed": True},
                event_log_path=Path(tmpdir) / "events.jsonl",
                parser=EmptyParser(),
                current_date=calendar_date(2026, 11, 1),
            )

        self.assertEqual(result[0]["status"], "PARSER_FAILED")
        self.assertIn("no executable request", result[0]["detail"])

    def test_cli_natural_language_request_uses_configured_timezone(self) -> None:
        class TimezoneParser(RequestParser):
            def parse(
                self,
                *,
                request_id: str,
                original_text: str,
                current_date: calendar_date,
                timezone_name: str,
            ) -> RequestParseResult:
                self.timezone_name = timezone_name
                return RequestParseResult(
                    requests=[
                        ParsedNaturalLanguageRequest(
                            program="aeroplan",
                            origin="JFK",
                            destination="CDG",
                            departure_date="2026-11-05",
                            cabin="Business",
                            adults=1,
                            trip_type="one_way",
                            maximum_points=70000,
                        )
                    ]
                )

        parser = TimezoneParser()
        request = {
            "request_id": "req-main-nl",
            "original_text": "Find one Aeroplan business seat from JFK to CDG next Thursday under 70k",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            request_path = directory / "request.json"
            confirmation_path = directory / "confirmation.json"
            event_log_path = directory / "events.jsonl"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            confirmation_path.write_text(json.dumps({"confirmed": False}), encoding="utf-8")
            with patch.object(
                sys,
                "argv",
                [
                    "flight_search_demo.app",
                    "--request",
                    str(request_path),
                    "--confirmation",
                    str(confirmation_path),
                    "--event-log",
                    str(event_log_path),
                    "--current-date",
                    "2026-11-01",
                    "--timezone",
                    "America/New_York",
                ],
            ), patch(
                "flight_search_demo.app.build_default_request_parser",
                return_value=parser,
            ):
                exit_code = main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(parser.timezone_name, "America/New_York")

    def test_material_edit_invalidates_natural_language_confirmation(self) -> None:
        class Parser(RequestParser):
            def parse(
                self,
                *,
                request_id: str,
                original_text: str,
                current_date: calendar_date,
                timezone_name: str,
            ) -> RequestParseResult:
                return RequestParseResult(
                    requests=[
                        ParsedNaturalLanguageRequest(
                            program="aeroplan",
                            origin="JFK",
                            destination="CDG",
                            departure_date="2026-11-05",
                            cabin="First",
                            adults=1,
                            trip_type="one_way",
                            maximum_points=70000,
                        )
                    ]
                )

        request = {
            "request_id": "req-invalidate",
            "original_text": "Find one Aeroplan business seat from JFK to CDG next Thursday under 70k",
        }
        stale_confirmation = build_confirmation_request(
            request=request,
            parsed_requests=[
                ParsedNaturalLanguageRequest(
                    program="aeroplan",
                    origin="JFK",
                    destination="CDG",
                    departure_date="2026-11-05",
                    cabin="Business",
                    adults=1,
                    trip_type="one_way",
                    maximum_points=70000,
                )
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_request(
                request=request,
                confirmation=stale_confirmation,
                event_log_path=Path(tmpdir) / "events.jsonl",
                parser=Parser(),
                current_date=calendar_date(2026, 11, 1),
            )

        self.assertEqual(result[0]["status"], "CONFIRMATION_REQUIRED")
        self.assertIn("request_hash", result[0])

    def test_structured_json_path_bypasses_parser_calls(self) -> None:
        class RaisingParser(RequestParser):
            def parse(
                self,
                *,
                request_id: str,
                original_text: str,
                current_date: calendar_date,
                timezone_name: str,
            ) -> RequestParseResult:
                raise AssertionError("structured JSON path must not call the parser")

        request = {
            "request_id": "req-structured-no-model",
            "original_text": "Aeroplan JFK to CDG on 2026-11-05 in business under 70000 points",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }
        confirmation = {
            "request_id": request["request_id"],
            "request_hash": build_request_hash(
                request["request_id"], request["original_text"], normalize_request(request)
            ),
            "confirmed": True,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_request(
                request=request,
                confirmation=confirmation,
                event_log_path=Path(tmpdir) / "events.jsonl",
                parser=RaisingParser(),
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "MATCH_FOUND")

    def test_run_uses_injected_current_date_and_utc_clock(self) -> None:
        request = {
            "request_id": "req-clock",
            "original_text": "Search using a deterministic clock",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }
        confirmation = {
            "request_id": request["request_id"],
            "request_hash": build_request_hash(
                request["request_id"],
                request["original_text"],
                normalize_request(request, current_date=calendar_date(2026, 11, 1)),
            ),
            "confirmed": True,
        }
        fixed_time = datetime(
            2030, 1, 2, 5, 4, 3, tzinfo=timezone(timedelta(hours=2))
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_structured_request(
                request=request,
                confirmation=confirmation,
                event_log_path=Path(tmpdir) / "events.jsonl",
                current_date=calendar_date(2026, 11, 6),
                clock=lambda: fixed_time,
            )

        self.assertEqual(result["status"], "UNSUPPORTED_REQUEST")
        self.assertIn("past", result["detail"])
        self.assertEqual(result["timestamp"], "2030-01-02T03:04:03+00:00")

    def test_execution_selects_registered_provider_adapter(self) -> None:
        class RecordingAdapter(AwardProviderAdapter):
            def execute(
                self, criteria: NormalizedCriteria, atomic_task_id: str
            ) -> Dict[str, str]:
                self.call = (criteria, atomic_task_id)
                return {"status": "ADAPTER_SELECTED", "detail": "registry adapter ran"}

        adapter = RecordingAdapter()
        adapter_registry: Mapping[str, AwardProviderAdapter] = {
            "fixture_rewards": adapter
        }
        request = {
            "request_id": "req-registry",
            "original_text": "Use the registered fixture rewards adapter",
            "program": "fixture_rewards",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }
        criteria = normalize_request(request, adapter_registry=adapter_registry)
        confirmation = {
            "request_id": request["request_id"],
            "request_hash": build_request_hash(
                request["request_id"], request["original_text"], criteria
            ),
            "confirmed": True,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_structured_request(
                request=request,
                confirmation=confirmation,
                event_log_path=Path(tmpdir) / "events.jsonl",
                adapter_registry=adapter_registry,
            )

        self.assertIsInstance(DEFAULT_ADAPTER_REGISTRY["aeroplan"], AeroplanFixtureAdapter)
        self.assertEqual(adapter.call[0], criteria)
        self.assertEqual(adapter.call[1], result["atomic_task_id"])
        self.assertEqual(result["status"], "ADAPTER_SELECTED")

    def test_known_award_above_caller_limit_is_reported_explicitly(self) -> None:
        request = {
            "request_id": "req-over-limit",
            "original_text": "Aeroplan JFK to CDG business under 59999 points",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 59999,
        }
        criteria = normalize_request(request)
        confirmation = {
            "request_id": request["request_id"],
            "request_hash": build_request_hash(
                request["request_id"], request["original_text"], criteria
            ),
            "confirmed": True,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_structured_request(
                request=request,
                confirmation=confirmation,
                event_log_path=Path(tmpdir) / "events.jsonl",
            )

        self.assertEqual(result["status"], "ABOVE_POINTS_LIMIT")
        self.assertIn("60000", result["detail"])

    def test_event_append_repairs_record_boundary_and_fsyncs_before_return(self) -> None:
        request = {
            "request_id": "req-durable",
            "original_text": "Durably record this Aeroplan search",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }
        criteria = normalize_request(request)
        confirmation = {
            "request_id": request["request_id"],
            "request_hash": build_request_hash(
                request["request_id"], request["original_text"], criteria
            ),
            "confirmed": True,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            event_log_path = Path(tmpdir) / "events.jsonl"
            event_log_path.write_text('{"existing": true}', encoding="utf-8")
            with patch("os.fsync") as fsync:
                result = run_structured_request(
                    request=request,
                    confirmation=confirmation,
                    event_log_path=event_log_path,
                )
                fsync.assert_called_once()
            records = [
                json.loads(line)
                for line in event_log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(records, [{"existing": True}, result])

    def test_terminal_report_contains_original_text_and_every_normalized_criterion(self) -> None:
        request = {
            "request_id": "req-terminal",
            "original_text": "  Find a business award from jfk to cdg  ",
            "program": "AEROPLAN",
            "origin": "jfk",
            "destination": "cdg",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "ONE_WAY",
            "maximum_points": 70000,
        }
        criteria = normalize_request(request)
        confirmation = {
            "request_id": request["request_id"],
            "request_hash": build_request_hash(
                request["request_id"], request["original_text"], criteria
            ),
            "confirmed": True,
        }
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir, redirect_stdout(stdout):
            run_structured_request(
                request=request,
                confirmation=confirmation,
                event_log_path=Path(tmpdir) / "events.jsonl",
            )

        report = stdout.getvalue()
        self.assertIn("original_text=Find a business award from jfk to cdg", report)
        for field, value in {
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "Business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }.items():
            with self.subTest(field=field):
                self.assertIn(f"{field}={value}", report)

    def test_airports_are_ascii_and_supported_by_the_fixture(self) -> None:
        for airport in ("ÅBC", "QQQ"):
            with self.subTest(airport=airport):
                with self.assertRaisesRegex(ValueError, "supported three-letter IATA"):
                    normalize_airport(airport, "origin")

    def test_departure_date_requires_exact_calendar_date_syntax(self) -> None:
        with patch("flight_search_demo.app.date") as date_parser:
            date_parser.fromisoformat.return_value = calendar_date(2026, 11, 5)
            for invalid_date in ("20261105", "2026-W45-4"):
                with self.subTest(invalid_date=invalid_date):
                    with self.assertRaisesRegex(ValueError, "exact YYYY-MM-DD"):
                        normalize_departure_date(invalid_date)

    def test_cli_process_reads_files_reports_and_records_success(self) -> None:
        request = {
            "request_id": "req-cli",
            "original_text": "Find one Aeroplan business seat from JFK to CDG",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }
        confirmation = {
            "request_id": request["request_id"],
            "request_hash": build_request_hash(
                request["request_id"], request["original_text"], normalize_request(request)
            ),
            "confirmed": True,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            request_path = directory / "request.json"
            confirmation_path = directory / "confirmation.json"
            event_log_path = directory / "events.jsonl"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            confirmation_path.write_text(json.dumps(confirmation), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "flight_search_demo.app",
                    "--request",
                    str(request_path),
                    "--confirmation",
                    str(confirmation_path),
                    "--event-log",
                    str(event_log_path),
                ],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            event = json.loads(event_log_path.read_text(encoding="utf-8"))

        self.assertEqual(completed.returncode, 0)
        self.assertIn(request["original_text"], completed.stdout)
        self.assertEqual(event["status"], "MATCH_FOUND")
        self.assertEqual(event["original_text"], request["original_text"])

    def test_cli_fixture_date_remains_executable_after_fixture_date(self) -> None:
        request = {
            "request_id": "req-cli-stable-fixture",
            "original_text": "Find one Aeroplan business seat from JFK to CDG",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }
        criteria = normalize_request(
            request,
            current_date=calendar_date(2026, 11, 1),
        )
        confirmation = {
            "request_id": request["request_id"],
            "request_hash": build_request_hash(
                request["request_id"], request["original_text"], criteria
            ),
            "confirmed": True,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            request_path = directory / "request.json"
            confirmation_path = directory / "confirmation.json"
            event_log_path = directory / "events.jsonl"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            confirmation_path.write_text(json.dumps(confirmation), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "flight_search_demo.app",
                    "--request",
                    str(request_path),
                    "--confirmation",
                    str(confirmation_path),
                    "--event-log",
                    str(event_log_path),
                    "--current-date",
                    "2026-11-01",
                ],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0)

    def test_cli_domain_failure_is_reported_recorded_and_exits_nonzero(self) -> None:
        request = {
            "request_id": "req-cli-failure",
            "original_text": "Use an unsupported airport",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "LHR",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            request_path = directory / "request.json"
            confirmation_path = directory / "confirmation.json"
            event_log_path = directory / "events.jsonl"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            confirmation_path.write_text(json.dumps({"confirmed": False}), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "flight_search_demo.app",
                    "--request",
                    str(request_path),
                    "--confirmation",
                    str(confirmation_path),
                    "--event-log",
                    str(event_log_path),
                ],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            event = json.loads(event_log_path.read_text(encoding="utf-8"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("UNSUPPORTED_REQUEST", completed.stdout)
        self.assertEqual(event["status"], "UNSUPPORTED_REQUEST")

    def test_structured_request_runs_through_confirmation_fixture_and_reports(self) -> None:
        request = {
            "request_id": "req-123",
            "original_text": "Aeroplan JFK to CDG on 2026-11-05 in business under 70000 points",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            event_log_path = Path(tmpdir) / "events.jsonl"
            confirmation = {
                "request_id": "req-123",
                "request_hash": build_request_hash(
                    "req-123", request["original_text"], normalize_request(request)
                ),
                "confirmed": True,
            }
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                result = run_structured_request(
                    request=request,
                    confirmation=confirmation,
                    event_log_path=event_log_path,
                )
                second_result = run_structured_request(
                    request=request,
                    confirmation=confirmation,
                    event_log_path=event_log_path,
                )

            events = event_log_path.read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(result["status"], "MATCH_FOUND")
        self.assertEqual(second_result["status"], "MATCH_FOUND")
        self.assertEqual(result["program"], "aeroplan")
        self.assertEqual(result["request_id"], "req-123")
        self.assertEqual(result["normalized_criteria"]["cabin"], "Business")
        self.assertIn("req-123", stdout.getvalue())
        self.assertIn(result["atomic_task_id"], stdout.getvalue())
        self.assertEqual(len(events), 2)
        event = json.loads(events[0])
        second_event = json.loads(events[1])
        self.assertEqual(event["request_id"], "req-123")
        self.assertEqual(event["original_text"], request["original_text"])
        self.assertEqual(event["atomic_task_id"], result["atomic_task_id"])
        self.assertEqual(event["status"], "MATCH_FOUND")
        self.assertEqual(event["normalized_criteria"], result["normalized_criteria"])
        self.assertEqual(second_event["atomic_task_id"], second_result["atomic_task_id"])

    def test_material_edit_invalidates_confirmation_hash(self) -> None:
        request = {
            "request_id": "req-123",
            "original_text": "Aeroplan JFK to CDG on 2026-11-05 in business under 70000 points",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }
        confirmation = {
            "request_id": "req-123",
            "request_hash": build_request_hash(
                "req-123", request["original_text"], normalize_request(request)
            ),
            "confirmed": True,
        }
        edited_request = dict(request)
        edited_request["maximum_points"] = 65000

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_structured_request(
                request=edited_request,
                confirmation=confirmation,
                event_log_path=Path(tmpdir) / "events.jsonl",
            )

        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")
        self.assertIn("request_hash", result["detail"])
        self.assertIsNone(result["atomic_task_id"])

    def test_original_text_edit_invalidates_confirmation_hash(self) -> None:
        request = {
            "request_id": "req-text",
            "original_text": "Aeroplan JFK to CDG on 2026-11-05 in business under 70000 points",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }
        confirmation = {
            "request_id": request["request_id"],
            "request_hash": build_request_hash(
                request["request_id"], request["original_text"], normalize_request(request)
            ),
            "confirmed": True,
        }
        edited_request = dict(request)
        edited_request["original_text"] = "Actually search LHR to CDG instead"

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_structured_request(
                request=edited_request,
                confirmation=confirmation,
                event_log_path=Path(tmpdir) / "events.jsonl",
            )

        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")

    def test_unsupported_structured_inputs_produce_explicit_non_success_outcomes(self) -> None:
        invalid_requests = [
            {"origin": "New York"},
            {"destination": "C"},
            {"departure_date": "11/05/2026"},
            {"departure_date": "2026-09-03"},
            {"cabin": "suite"},
            {"adults": 2},
            {"trip_type": "round_trip"},
            {"maximum_points": "70000"},
        ]
        base_request = {
            "request_id": "req-unsupported",
            "original_text": "bad input",
            "program": "aeroplan",
            "origin": "JFK",
            "destination": "CDG",
            "departure_date": "2026-11-05",
            "cabin": "business",
            "adults": 1,
            "trip_type": "one_way",
            "maximum_points": 70000,
        }
        confirmation = {
            "request_id": "req-unsupported",
            "request_hash": build_request_hash(
                "req-unsupported", base_request["original_text"], normalize_request(base_request)
            ),
            "confirmed": True,
        }

        for invalid_fields in invalid_requests:
            with self.subTest(invalid_fields=invalid_fields):
                request = dict(base_request)
                request.update(invalid_fields)
                with tempfile.TemporaryDirectory() as tmpdir:
                    result = run_structured_request(
                        request=request,
                        confirmation=confirmation,
                        event_log_path=Path(tmpdir) / "events.jsonl",
                    )
                self.assertEqual(result["status"], "UNSUPPORTED_REQUEST")
                self.assertIsNone(result["atomic_task_id"])


if __name__ == "__main__":
    unittest.main()
