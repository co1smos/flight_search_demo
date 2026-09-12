import json
import time
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


def test_live_driver_reserves_once_at_submission_and_preserves_non_availability_failures(tmp_path):
    request, confirmation = request_and_confirmation()

    class Driver:
        def execute(self, criteria, *, deadline_seconds, reserve_submission):
            assert deadline_seconds == 60
            assert criteria.origin == "JFK"
            assert reserve_submission() is True
            return {
                "status": "AUTHENTICATION_REQUIRED",
                "detail": "Aeroplan authentication is required",
                "live_validation_performed": True,
                "visible_results_validated": False,
                "profile_reusable": True,
            }

    event = validate_controlled_search(
        request=request,
        confirmation=confirmation,
        risk_acknowledged=True,
        event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3",
        current_date=date(2026, 9, 11),
        live_driver=Driver(),
    )

    assert event["status"] == "AUTHENTICATION_REQUIRED"
    assert event["status"] != "NO_AWARD_AVAILABILITY"
    assert event["live_search_submitted"] is True
    assert event["allowance_remaining"] == 9
    assert event["adapter_status"] == "UNVERIFIED"
    assert event["continuous_live_execution_enabled"] is False


def test_unavailable_configured_browser_is_the_exact_external_blocker(tmp_path):
    from flight_search_demo.aeroplan_validation import PersistentAeroplanDriver

    request, confirmation = request_and_confirmation()
    event = validate_controlled_search(
        request=request,
        confirmation=confirmation,
        risk_acknowledged=True,
        event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3",
        current_date=date(2026, 9, 11),
        live_driver=PersistentAeroplanDriver("http://127.0.0.2:3000"),
    )

    assert event["status"] == "MANUAL_SEARCH_ONLY"
    assert event["blocking_reason"] == "CONTROLLED_BROWSER_UNAVAILABLE"
    assert event["live_search_submitted"] is False
    assert event["allowance_remaining"] == 10


def test_persistent_driver_uses_configured_controlled_browser_and_detects_authentication(monkeypatch):
    import flight_search_demo.aeroplan_validation as validation
    from flight_search_demo.aeroplan import PageSnapshot

    request, _ = request_and_confirmation()
    criteria = normalize_request(request, current_date=date(2026, 9, 11))
    calls = []

    class Browser:
        def __init__(self, *, cdp_url, deadline_seconds, reserve_submission):
            self.navigation_performed = False
            calls.append((cdp_url, deadline_seconds, reserve_submission))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            calls.append("closed")

        def open(self, url):
            self.navigation_performed = True
            calls.append(url)
            return PageSnapshot(
                url,
                '<html><body><a>Sign in</a><footer>Air Canada</footer></body></html>',
            )

        def deterministic_search(self, criteria):
            raise AssertionError("authentication must be detected before submission")

        def browser_agent_search(self, *args, **kwargs):
            raise AssertionError("authentication must not invoke a model")

    monkeypatch.setattr(validation, "PersistentAeroplanBrowser", Browser)
    monkeypatch.setattr(
        validation,
        "discover_debugger_cdp_url",
        lambda base_url, api_key, timeout: "ws://127.0.0.1:9223/devtools/browser/test",
    )

    driver = validation.PersistentAeroplanDriver("http://127.0.0.1:3000")
    result = driver.execute(criteria, deadline_seconds=60, reserve_submission=lambda: True)

    assert result["status"] == "AUTHENTICATION_REQUIRED"
    assert result["live_validation_performed"] is True
    assert result["visible_results_validated"] is False
    assert result["profile_reusable"] is True
    assert calls[-1] == "closed"


def test_live_driver_cannot_report_no_availability_without_visible_validation(tmp_path):
    request, confirmation = request_and_confirmation()

    class Driver:
        def execute(self, criteria, *, deadline_seconds, reserve_submission):
            assert reserve_submission() is True
            return {
                "status": "NO_AWARD_AVAILABILITY",
                "detail": "empty extraction",
                "live_validation_performed": True,
                "visible_results_validated": False,
                "profile_reusable": True,
            }

    event = validate_controlled_search(
        request=request,
        confirmation=confirmation,
        risk_acknowledged=True,
        event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3",
        current_date=date(2026, 9, 11),
        live_driver=Driver(),
    )

    assert event["status"] == "PARSER_FAILED"
    assert event["adapter_status"] == "UNVERIFIED"
    assert event["allowance_remaining"] == 9


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


def test_controller_enforces_one_deadline_and_emits_correlated_timeout(tmp_path, monkeypatch):
    import flight_search_demo.aeroplan_validation as validation

    request, confirmation = request_and_confirmation()

    class BlockingDriver:
        def execute(self, criteria, *, deadline_seconds, reserve_submission):
            time.sleep(1)
            raise AssertionError("controller failed to interrupt the blocked driver")

    monkeypatch.setattr(validation, "OPERATION_DEADLINE_SECONDS", 0.05)
    started = time.monotonic()
    event = validate_controlled_search(
        request=request,
        confirmation=confirmation,
        risk_acknowledged=True,
        event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3",
        current_date=date(2026, 9, 11),
        live_driver=BlockingDriver(),
    )

    assert time.monotonic() - started < 0.5
    assert event["status"] == "SEARCH_TIMEOUT"
    assert event["blocking_reason"] == "SEARCH_TIMEOUT"
    assert event["request_id"] == request["request_id"]
    assert json.loads((tmp_path / "events.jsonl").read_text()) == event
