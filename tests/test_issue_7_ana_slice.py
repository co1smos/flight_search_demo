import json
from datetime import date
from pathlib import Path

import pytest

from flight_search_demo.app import build_request_hash, normalize_request


FIXTURES = Path(__file__).parent / "fixtures" / "ana"


@pytest.fixture
def criteria():
    request = {
        "request_id": "ana-fixture",
        "original_text": "ANA Mileage Club SFO to NRT 2026-09-17 business one adult one way under 80000 miles",
        "program": "ana",
        "origin": "SFO",
        "destination": "NRT",
        "departure_date": "2026-09-17",
        "cabin": "Business",
        "adults": 1,
        "trip_type": "one_way",
        "maximum_points": 80000,
    }
    return normalize_request(request, current_date=date(2026, 9, 12))


def test_browser_agent_completion_is_schema_validated(criteria):
    from flight_search_demo.ana import AnaFixtureBrowser, AnaSearchAdapter

    browser = AnaFixtureBrowser(
        FIXTURES / "match.html",
        initial_fixture=FIXTURES / "unknown.html",
        deterministic_supported=False,
    )
    result = AnaSearchAdapter(browser=browser).execute(criteria, "ana-agent")

    assert result["status"] == "MATCH_FOUND"
    assert result["browser_agent_used"] is True
    assert result["itineraries"][0]["points_per_passenger"] == 75000
    assert result["itineraries"][0]["segments"][0]["flight_number"] == "NH 107"


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("login_required.html", "AUTHENTICATION_REQUIRED"),
        ("session_error.html", "SESSION_ERROR"),
        ("heavy_traffic.html", "HEAVY_TRAFFIC_BLOCKED"),
        ("unknown.html", "PARSER_FAILED"),
        ("unverified_price.html", "UNVERIFIED_PRICE"),
        ("validation_rejection.html", "VALIDATION_REJECTED"),
    ],
)
def test_fixtures_distinguish_deterministic_terminal_states(criteria, fixture, expected):
    from flight_search_demo.ana import AnaFixtureBrowser, AnaSearchAdapter

    result = AnaSearchAdapter(browser=AnaFixtureBrowser(FIXTURES / fixture)).execute(
        criteria, "ana-state"
    )
    assert result["status"] == expected


@pytest.mark.parametrize(
    ("agent_status", "expected"),
    [
        ("step_exhausted", "AGENT_STEP_EXHAUSTED"),
        ("action_exhausted", "AGENT_ACTION_EXHAUSTED"),
        ("failure_exhausted", "AGENT_FAILURE_EXHAUSTED"),
    ],
)
def test_browser_agent_exhaustion_states_are_distinct(criteria, agent_status, expected):
    from flight_search_demo.ana import AnaAgentOutcome, AnaFixtureBrowser, AnaSearchAdapter

    browser = AnaFixtureBrowser(
        FIXTURES / "match.html",
        initial_fixture=FIXTURES / "unknown.html",
        deterministic_supported=False,
        agent_outcome=AnaAgentOutcome(
            status=agent_status,
            page=None,
            steps=2,
            failures=1,
            visited_urls=(),
            actions=(),
        ),
    )
    assert AnaSearchAdapter(browser=browser).execute(criteria, "bounded")["status"] == expected


def test_timeout_is_distinct_and_agent_never_runs(criteria):
    from flight_search_demo.ana import AnaFixtureBrowser, AnaSearchAdapter

    browser = AnaFixtureBrowser(FIXTURES / "match.html", timeout_at="open")
    result = AnaSearchAdapter(browser=browser).execute(criteria, "timeout")
    assert result["status"] == "SEARCH_TIMEOUT"
    assert browser.calls == ["open"]


def test_search_policy_confines_domains_paths_actions_and_criteria(criteria):
    from flight_search_demo.ana import AnaSearchPolicy, PolicyViolation

    policy = AnaSearchPolicy()
    policy.validate_url("https://www.ana.co.jp/en/us/amc/international-flight-awards/")
    for url in (
        "https://example.com/",
        "https://www.ana.co.jp/en/us/shopping/",
        "https://www.ana.co.jp/en/us/amc/international-flight-awards/?date=2026-09-18",
    ):
        with pytest.raises(PolicyViolation):
            policy.validate_url(url)
    for action in (
        {"action": "purchase"},
        {"action": "fill_search_field", "field": "payment_card", "value": "x"},
        {"action": "submit_search", "retry": True},
    ):
        with pytest.raises(PolicyViolation):
            policy.validate_action(action)
    policy.validate_criteria(criteria)
    with pytest.raises(PolicyViolation):
        policy.validate_criteria(criteria.__class__(**{**criteria.__dict__, "trip_type": "round_trip"}))


def _request_and_confirmation():
    request = {
        "request_id": "controlled-ana-7",
        "original_text": "Confirmed ANA Mileage Club award search",
        "program": "ana",
        "origin": "SFO",
        "destination": "NRT",
        "departure_date": "2026-09-17",
        "cabin": "Business",
        "adults": 1,
        "trip_type": "one_way",
        "maximum_points": 80000,
    }
    criteria = normalize_request(request, current_date=date(2026, 9, 12))
    confirmation = {
        "confirmed": True,
        "request_id": request["request_id"],
        "request_hash": build_request_hash(request["request_id"], request["original_text"], criteria),
    }
    return request, confirmation


def test_ana_allowance_is_independent_and_persistent(tmp_path):
    from flight_search_demo.aeroplan_validation import AeroplanAllowance
    from flight_search_demo.ana_validation import AnaAllowance

    path = tmp_path / "usage.sqlite3"
    assert AeroplanAllowance(path).remaining == 10
    ana = AnaAllowance(path)
    assert ana.remaining == 10
    assert ana.consume_submission() is True
    assert AnaAllowance(path).remaining == 9
    assert AeroplanAllowance(path).remaining == 10


def test_default_live_driver_uses_an_ana_only_browser_endpoint_and_profile():
    from flight_search_demo.ana_validation import DEFAULT_ANA_PROFILE, PersistentAnaDriver

    driver = PersistentAnaDriver()
    assert driver.steel_base_url == "http://127.0.0.1:3001"
    assert driver.profile_path == DEFAULT_ANA_PROFILE
    assert "ana-browser-profile" in str(driver.profile_path)


def test_controlled_validation_is_correlated_and_fail_closed(tmp_path):
    from flight_search_demo.ana_validation import validate_controlled_ana_search

    request, confirmation = _request_and_confirmation()
    event = validate_controlled_ana_search(
        request=request,
        confirmation=confirmation,
        risk_acknowledged=True,
        event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3",
        current_date=date(2026, 9, 12),
    )
    assert event["status"] == "MANUAL_SEARCH_ONLY"
    assert event["adapter_status"] == "UNVERIFIED"
    assert event["continuous_live_execution_enabled"] is False
    assert event["allowance_remaining"] == 10
    assert event["atomic_task_id"].startswith("ana-")
    assert json.loads((tmp_path / "events.jsonl").read_text()) == event


def test_live_availability_requires_submission_visible_validation_and_reusable_profile(tmp_path):
    from flight_search_demo.ana_validation import validate_controlled_ana_search

    request, confirmation = _request_and_confirmation()

    class Driver:
        def execute(self, criteria, *, deadline_seconds, reserve_submission):
            assert 0 < deadline_seconds <= 90
            assert reserve_submission() is True
            return {
                "status": "MATCH_FOUND",
                "detail": "claimed match",
                "live_validation_performed": True,
                "visible_results_validated": False,
                "profile_reusable": True,
            }

    event = validate_controlled_ana_search(
        request=request,
        confirmation=confirmation,
        risk_acknowledged=True,
        event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3",
        current_date=date(2026, 9, 12),
        live_driver=Driver(),
    )
    assert event["status"] == "VALIDATION_REJECTED"
    assert event["allowance_remaining"] == 9
    assert event["adapter_status"] == "UNVERIFIED"
