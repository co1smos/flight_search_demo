import io
import json
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from flight_search_demo.aeroplan import (
    AeroplanFixtureBrowser,
    AeroplanSearchAdapter,
    BrowserAgentOutcome,
    OFFICIAL_SEARCH_ENTRY_URL,
    PageSnapshot,
    PolicyViolation,
    SearchPolicy,
)
from flight_search_demo.app import (
    AeroplanFixtureAdapter,
    NormalizedCriteria,
    build_request_hash,
    run_structured_request,
)


FIXTURES = Path(__file__).parent / "fixtures" / "aeroplan"


@pytest.fixture
def criteria():
    return NormalizedCriteria(
        program="aeroplan",
        origin="JFK",
        destination="CDG",
        departure_date="2026-11-05",
        cabin="Business",
        adults=1,
        trip_type="one_way",
        maximum_points=70000,
    )


def test_complete_visible_itinerary_qualifies_through_deterministic_search(criteria):
    browser = AeroplanFixtureBrowser(FIXTURES / "match.html")
    adapter = AeroplanSearchAdapter(browser=browser)

    result = adapter.execute(criteria, "aeroplan-test")

    assert result["status"] == "MATCH_FOUND"
    assert result["search_entry_url"] == OFFICIAL_SEARCH_ENTRY_URL
    assert result["itineraries"][0]["points_per_passenger"] == 60000
    assert result["itineraries"][0]["cash"] == {
        "displayed_total": "$82.40",
        "currency": "CAD",
    }
    assert browser.calls == ["open", "deterministic_search"]
    assert result["itineraries"][0]["segments"][0]["operating_carrier"] == "Air Canada"


def test_default_fixture_adapter_uses_the_validated_pipeline(criteria):
    result = AeroplanFixtureAdapter().execute(criteria, "default-fixture")
    assert result["status"] == "MATCH_FOUND"
    assert result["search_entry_url"] == OFFICIAL_SEARCH_ENTRY_URL
    assert result["itineraries"][0]["segments"][0]["flight_number"] == "AC 872"


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("no_availability.html", "NO_AWARD_AVAILABILITY"),
        ("above_limit.html", "ABOVE_POINTS_LIMIT"),
        ("unverified_price.html", "UNVERIFIED_PRICE"),
        ("authentication.html", "AUTHENTICATION_REQUIRED"),
        ("challenge.html", "CHALLENGE_BLOCKED"),
        ("site_error.html", "SITE_ERROR"),
        ("parser_failure.html", "PARSER_FAILED"),
    ],
)
def test_controlled_fixtures_have_distinct_terminal_outcomes(criteria, fixture, expected):
    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(FIXTURES / fixture)
    ).execute(criteria, "fixture-status")
    assert result["status"] == expected


def test_timeout_is_terminal_and_never_invokes_agent(criteria):
    browser = AeroplanFixtureBrowser(FIXTURES / "timeout.html", timeout_at="open")
    result = AeroplanSearchAdapter(browser=browser).execute(criteria, "timeout")
    assert result["status"] == "SEARCH_TIMEOUT"
    assert browser.calls == ["open"]


def test_unknown_non_security_layout_uses_bounded_agent_only_after_deterministic(criteria):
    browser = AeroplanFixtureBrowser(
        FIXTURES / "match.html",
        initial_fixture=FIXTURES / "unknown.html",
        deterministic_supported=False,
    )
    result = AeroplanSearchAdapter(browser=browser, max_agent_steps=2).execute(criteria, "agent")
    assert result["status"] == "MATCH_FOUND"
    assert result["browser_agent_used"] is True
    assert browser.calls == ["open", "deterministic_search", "browser_agent_search"]


def test_security_pages_do_not_invoke_deterministic_or_agent_actions(criteria):
    browser = AeroplanFixtureBrowser(FIXTURES / "authentication.html")
    result = AeroplanSearchAdapter(browser=browser).execute(criteria, "auth")
    assert result["status"] == "AUTHENTICATION_REQUIRED"
    assert browser.calls == ["open"]


def test_agent_outcome_cannot_bypass_deterministic_extraction_validation(criteria):
    malformed = PageSnapshot(
        OFFICIAL_SEARCH_ENTRY_URL,
        (FIXTURES / "unverified_price.html").read_text(encoding="utf-8"),
    )
    outcome = BrowserAgentOutcome(
        status="completed",
        page=malformed,
        steps=2,
        visited_urls=(OFFICIAL_SEARCH_ENTRY_URL,),
        actions=({"action": "read_results"},),
    )
    browser = AeroplanFixtureBrowser(
        FIXTURES / "match.html",
        initial_fixture=FIXTURES / "unknown.html",
        deterministic_supported=False,
        agent_outcome=outcome,
    )
    result = AeroplanSearchAdapter(browser=browser).execute(criteria, "agent-validation")
    assert result["status"] == "UNVERIFIED_PRICE"


def test_price_must_be_visible_as_an_exact_value_not_a_numeric_substring(criteria, tmp_path):
    fixture = tmp_path / "substring-price.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <div>160,000 pts + $82.40 CAD</div>
        <script id="aeroplan-results-data" type="application/json">
        {"itineraries":[{"visible":true,"complete_itinerary":true,
        "price_kind":"exact","price_label":"60,000 pts","points_per_passenger":60000,
        "cash":{"displayed_total":"$82.40","currency":"CAD"},
        "segments":[{"departure":"2026-11-05T20:30:00-05:00",
        "arrival":"2026-11-06T08:35:00+01:00","flight_number":"AC 872",
        "marketing_carrier":"Air Canada","operating_carrier":"Air Canada",
        "cabin":"Business"}]}]}
        </script></main></body></html>""",
        encoding="utf-8",
    )

    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(fixture)
    ).execute(criteria, "substring-price")

    assert result["status"] == "UNVERIFIED_PRICE"


@pytest.mark.parametrize(
    "action",
    [
        {"action": "enter_credentials"},
        {"action": "change_account"},
        {"action": "transfer_points"},
        {"action": "enter_passenger_details"},
        {"action": "enter_payment"},
        {"action": "book_itinerary"},
        {"action": "execute_script"},
        {"action": "navigate", "url": "https://example.com/flights"},
        {"action": "navigate", "url": "https://www.aircanada.com/checkout"},
    ],
)
def test_search_policy_rejects_non_search_and_unsupported_actions(action):
    with pytest.raises(PolicyViolation):
        SearchPolicy().validate_action(action)


@pytest.mark.parametrize(
    "action",
    [
        {"action": "fill_search_field", "field": "password", "value": "redacted"},
        {"action": "fill_search_field", "field": "passenger_name", "value": "Jane Doe"},
        {"action": "fill_search_field", "field": "first_name", "value": "Jane"},
        {"action": "fill_search_field", "field": "credit_card", "value": "redacted"},
        {"action": "select_search_option", "field": "transfer_points", "value": "yes"},
        {"action": "submit_search", "selector": "button[data-action='book']"},
        {"action": "read_results", "script": "document.body.innerText"},
    ],
)
def test_search_policy_rejects_forbidden_intent_hidden_in_allowed_actions(action):
    with pytest.raises(PolicyViolation):
        SearchPolicy().validate_action(action)


def test_search_policy_allows_passenger_count_as_search_criteria():
    SearchPolicy().validate_action(
        {"action": "select_search_option", "field": "passenger_count", "value": 2}
    )


def test_search_policy_custom_allowlist_cannot_expand_beyond_approved_domains():
    with pytest.raises(PolicyViolation):
        SearchPolicy(allowed_domains=["www.aircanada.com", "example.com"])


@pytest.mark.parametrize(
    "url",
    [
        "https://www.aircanada.com/aeroplan/redeem/availability-evil",
        "https://www.aircanada.com/search-and-book",
        "https://www.aircanada.com:444/aeroplan/redeem/availability",
    ],
)
def test_search_policy_rejects_lookalike_paths_and_non_https_default_ports(url):
    with pytest.raises(PolicyViolation):
        SearchPolicy().validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://login.aircanada.com/account/delete",
        "https://aircanada.b2clogin.com/password-reset",
    ],
)
def test_search_policy_rejects_agent_navigation_to_identity_domains(url):
    with pytest.raises(PolicyViolation):
        SearchPolicy().validate_action({"action": "navigate", "url": url})


def test_agent_outcome_is_rejected_when_step_bound_or_domain_policy_is_broken(criteria):
    for outcome in (
        BrowserAgentOutcome("completed", None, 7, (), (), "too many steps"),
        BrowserAgentOutcome(
            "completed",
            PageSnapshot(
                OFFICIAL_SEARCH_ENTRY_URL,
                (FIXTURES / "match.html").read_text(encoding="utf-8"),
            ),
            1,
            (OFFICIAL_SEARCH_ENTRY_URL,),
            ({"action": "read_results"}, {"action": "read_results"}),
            "more actions than bounded steps",
        ),
        BrowserAgentOutcome(
            "completed",
            PageSnapshot(
                OFFICIAL_SEARCH_ENTRY_URL,
                (FIXTURES / "match.html").read_text(encoding="utf-8"),
            ),
            1,
            (),
            ({"action": "read_results"},),
            "final page missing from navigation trace",
        ),
        BrowserAgentOutcome(
            "completed",
            None,
            1,
            ("https://example.com/",),
            ({"action": "read_results"},),
        ),
    ):
        browser = AeroplanFixtureBrowser(
            FIXTURES / "match.html",
            initial_fixture=FIXTURES / "unknown.html",
            deterministic_supported=False,
            agent_outcome=outcome,
        )
        result = AeroplanSearchAdapter(browser=browser, max_agent_steps=2).execute(criteria, "policy")
        assert result["status"] == "BROWSER_AGENT_FAILED"


def test_connections_segments_and_mixed_cabin_warning_are_reported_but_do_not_qualify(criteria):
    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(FIXTURES / "mixed_cabin.html")
    ).execute(criteria, "mixed")
    assert result["status"] == "NO_QUALIFYING_ITINERARY"
    assert result["quality_warnings"] == ["mixed_cabin"]
    assert result["itineraries"][0]["connections"] == 1
    assert len(result["itineraries"][0]["segments"]) == 2


def test_points_limit_is_per_passenger_and_cash_is_not_converted(criteria):
    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(FIXTURES / "connection.html")
    ).execute(replace(criteria, adults=2), "two-adults")
    itinerary = result["itineraries"][0]
    assert result["status"] == "MATCH_FOUND"
    assert itinerary["points_per_passenger"] == 65000
    assert itinerary["points_total"] == 130000
    assert itinerary["cash"] == {"displayed_total": "US$146.80", "currency": "USD"}
    assert itinerary["connections"] == 1
    assert len(itinerary["segments"]) == 2


def test_app_json_and_terminal_reports_use_official_entry_not_transient_url(criteria):
    request = {
        "request_id": "issue-5-report",
        "original_text": "Aeroplan JFK to CDG 2026-11-05 business one adult under 70000 points",
        **criteria.__dict__,
    }
    confirmation = {
        "confirmed": True,
        "request_id": request["request_id"],
        "request_hash": build_request_hash(request["request_id"], request["original_text"], criteria),
    }
    adapter = AeroplanSearchAdapter(browser=AeroplanFixtureBrowser(FIXTURES / "match.html"))
    with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as terminal:
        path = Path(directory) / "events.jsonl"
        event = run_structured_request(
            request=request,
            confirmation=confirmation,
            event_log_path=path,
            adapter_registry={"aeroplan": adapter},
            current_date=date(2026, 11, 1),
        )
        persisted = json.loads(path.read_text(encoding="utf-8"))
    assert event["search_entry_url"] == OFFICIAL_SEARCH_ENTRY_URL
    assert persisted["search_entry_url"] == OFFICIAL_SEARCH_ENTRY_URL
    assert OFFICIAL_SEARCH_ENTRY_URL in terminal.getvalue()
    assert "session" not in terminal.getvalue().lower()
