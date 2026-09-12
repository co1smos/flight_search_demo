import json
from datetime import date

from flight_search_demo.aeroplan_validation import validate_controlled_search
from flight_search_demo.app import build_request_hash, normalize_request


def request_and_confirmation():
    request = dict(request_id="controlled-6", original_text="Confirmed award search",
                   program="aeroplan", origin="JFK", destination="CDG",
                   departure_date="2026-11-05", cabin="Business", adults=1,
                   trip_type="one_way", maximum_points=70000)
    criteria = normalize_request(request, current_date=date(2026, 9, 11))
    confirmation = dict(confirmed=True, request_id=request["request_id"],
                        request_hash=build_request_hash(request["request_id"], request["original_text"], criteria))
    return request, confirmation


def test_blocked_validation_is_correlated_and_never_fixture_success(tmp_path):
    request, confirmation = request_and_confirmation()
    event = validate_controlled_search(request=request, confirmation=confirmation,
        risk_acknowledged=True, event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3", current_date=date(2026, 9, 11))
    assert event["status"] == "MANUAL_SEARCH_ONLY"
    assert event["adapter_status"] == "UNVERIFIED"
    assert event["continuous_live_execution_enabled"] is False
    assert event["live_search_submitted"] is False
    assert event["request_id"] == request["request_id"]
    assert event["atomic_task_id"].startswith("aeroplan-")
    assert event["allowance_remaining"] == 10
    assert json.loads((tmp_path / "events.jsonl").read_text()) == event


def test_risk_and_request_confirmation_are_independent_gates(tmp_path):
    request, confirmation = request_and_confirmation()
    for acknowledgement, confirmed, expected in [(False, True, "RISK_ACKNOWLEDGEMENT_REQUIRED"),
                                                (True, False, "CONFIRMATION_REQUIRED")]:
        event = validate_controlled_search(request=request,
            confirmation={**confirmation, "confirmed": confirmed},
            risk_acknowledged=acknowledgement, event_log_path=tmp_path / "events.jsonl",
            allowance_db_path=tmp_path / "usage.sqlite3", current_date=date(2026, 9, 11))
        assert event["status"] == expected
        assert event["live_search_submitted"] is False


def test_empty_extraction_is_incomplete_not_no_availability(tmp_path):
    from flight_search_demo.aeroplan import AeroplanFixtureBrowser, AeroplanSearchAdapter
    request, _ = request_and_confirmation()
    criteria = normalize_request(request, current_date=date(2026, 9, 11))
    fixture = tmp_path / "empty.html"
    fixture.write_text('<main data-page-kind="results"><script id="aeroplan-results-data" '
                       'type="application/json">{"itineraries": []}</script></main>')
    result = AeroplanSearchAdapter(browser=AeroplanFixtureBrowser(fixture)).execute(criteria, "empty")
    assert result["status"] == "PARSER_FAILED"


def test_allowance_persists_without_refunds_or_reset(tmp_path):
    from flight_search_demo.aeroplan_validation import AeroplanAllowance
    path = tmp_path / "usage.sqlite3"
    allowance = AeroplanAllowance(path)
    assert allowance.remaining == 10
    assert allowance.consume_submission() is True
    # Reopening after a timeout/restart cannot refund the dispatch reservation.
    reopened = AeroplanAllowance(path)
    assert reopened.remaining == 9
    for _ in range(9):
        assert reopened.consume_submission() is True
    assert allowance.consume_submission() is False
    assert AeroplanAllowance(path).remaining == 0


def test_changed_criteria_cannot_reuse_confirmation(tmp_path):
    request, confirmation = request_and_confirmation()
    event = validate_controlled_search(request={**request, "destination": "LHR"},
        confirmation=confirmation, risk_acknowledged=True,
        event_log_path=tmp_path / "events.jsonl", allowance_db_path=tmp_path / "usage.sqlite3",
        current_date=date(2026, 9, 11))
    assert event["status"] == "CONFIRMATION_REQUIRED"
    assert event["allowance_remaining"] == 10


def test_authorized_blocked_report_distinguishes_driver_from_operator_gate(tmp_path):
    request, confirmation = request_and_confirmation()
    event = validate_controlled_search(request=request, confirmation=confirmation,
        risk_acknowledged=True, event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3", current_date=date(2026, 9, 11))
    assert event["risk_acknowledged"] is True
    assert event["request_confirmed"] is True
    assert event["blocking_reason"] == "LIVE_DRIVER_UNAVAILABLE"
    assert event["live_validation_performed"] is False


def test_unconfirmed_report_does_not_claim_operator_gate_passed(tmp_path):
    request, confirmation = request_and_confirmation()
    event = validate_controlled_search(request={**request, "destination": "LHR"},
        confirmation=confirmation, risk_acknowledged=True,
        event_log_path=tmp_path / "events.jsonl", allowance_db_path=tmp_path / "usage.sqlite3",
        current_date=date(2026, 9, 11))
    assert event["request_confirmed"] is False
    assert event["blocking_reason"] == "CONFIRMATION_REQUIRED"


def test_malformed_confirmation_fails_closed_with_a_correlated_report(tmp_path):
    request, _ = request_and_confirmation()
    event = validate_controlled_search(request=request, confirmation=[],
        risk_acknowledged=True, event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3", current_date=date(2026, 9, 11))
    assert event["status"] == "CONFIRMATION_REQUIRED"
    assert event["request_confirmed"] is False
    assert event["live_search_submitted"] is False
    assert event["allowance_remaining"] == 10
    assert json.loads((tmp_path / "events.jsonl").read_text()) == event


def test_malformed_request_fails_closed_without_consuming_allowance(tmp_path):
    event = validate_controlled_search(request=[], confirmation={},
        risk_acknowledged=True, event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3", current_date=date(2026, 9, 11))
    assert event["status"] == "UNSUPPORTED_REQUEST"
    assert event["request_confirmed"] is False
    assert event["live_search_submitted"] is False
    assert event["allowance_remaining"] == 10
