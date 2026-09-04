from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flight_search_demo.app import build_request_hash, normalize_request, run_structured_request


class Issue3ApplicationTests(unittest.TestCase):
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
                "request_hash": build_request_hash("req-123", normalize_request(request)),
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
            "request_hash": build_request_hash("req-123", normalize_request(request)),
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

    def test_unsupported_structured_inputs_produce_explicit_non_success_outcomes(self) -> None:
        invalid_requests = [
            {"program": "ana"},
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
            "request_hash": build_request_hash("req-unsupported", normalize_request(base_request)),
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
