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
        ("non_object_payload.html", "PARSER_FAILED"),
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


def test_exact_visible_price_can_span_nested_inline_nodes(criteria, tmp_path):
    fixture = tmp_path / "nested-price.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <article><div>AC 872</div><div><span>60,000</span> <span>pts</span> + <span>$82.40</span> <span>CAD</span></div></article>
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
    ).execute(criteria, "nested-price")

    assert result["status"] == "MATCH_FOUND"


def test_exact_visible_price_can_be_a_standalone_inline_element(criteria, tmp_path):
    fixture = tmp_path / "inline-price.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <div>Flight AC 872 <span>60,000 pts + $82.40 CAD</span></div>
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
    ).execute(criteria, "inline-price")

    assert result["status"] == "MATCH_FOUND"


def test_qualified_price_in_standalone_inline_element_is_rejected(criteria, tmp_path):
    fixture = tmp_path / "qualified-inline-price.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <div>Flight AC 872 from <span>60,000 pts + $82.40 CAD</span></div>
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
    ).execute(criteria, "qualified-inline-price")

    assert result["status"] == "UNVERIFIED_PRICE"


def test_nested_inline_price_with_visible_qualifier_is_rejected(criteria, tmp_path):
    fixture = tmp_path / "nested-qualified-price.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <div><span>From:</span> <span>60,000</span> <span>pts</span> + <span>$82.40 CAD</span></div>
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
    ).execute(criteria, "nested-qualified-price")

    assert result["status"] == "UNVERIFIED_PRICE"


def test_split_sibling_from_qualifier_is_rejected(criteria, tmp_path):
    fixture = tmp_path / "split-sibling-from-price.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <article><div>From</div><div>60,000 pts + $82.40 CAD</div></article>
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
    ).execute(criteria, "split-sibling-from-price")

    assert result["status"] == "UNVERIFIED_PRICE"


def test_nested_price_wrapper_uses_full_itinerary_card_context(criteria, tmp_path):
    fixture = tmp_path / "nested-wrapper-from-price.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <article><div>From</div><section><div>60,000 pts + $82.40 CAD</div></section></article>
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
    ).execute(criteria, "nested-wrapper-from-price")

    assert result["status"] == "UNVERIFIED_PRICE"


def test_payload_price_is_verified_against_its_own_itinerary_card(criteria, tmp_path):
    fixture = tmp_path / "separate-card-price.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <article><div>AC 111</div><div>60,000 pts + $82.40 CAD</div></article>
        <article><div>AC 872</div><div>From 60,000 pts + $82.40 CAD</div></article>
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
    ).execute(criteria, "separate-card-price")

    assert result["status"] == "UNVERIFIED_PRICE"


@pytest.mark.parametrize("unrelated_flight", ["UAL 111", "AC 111A"])
def test_payload_price_rejects_other_complete_flight_number_grammars(
    criteria, tmp_path, unrelated_flight
):
    fixture = tmp_path / "other-flight-number-price.html"
    fixture.write_text(
        f"""<!doctype html><html><body><main data-page-kind="results">
        <article><div>{unrelated_flight}</div><div>60,000 pts + $82.40 CAD</div></article>
        <script id="aeroplan-results-data" type="application/json">
        {{"itineraries":[{{"visible":true,"complete_itinerary":true,
        "price_kind":"exact","price_label":"60,000 pts","points_per_passenger":60000,
        "cash":{{"displayed_total":"$82.40","currency":"CAD"}},
        "segments":[{{"departure":"2026-11-05T20:30:00-05:00",
        "arrival":"2026-11-06T08:35:00+01:00","flight_number":"AC 872",
        "marketing_carrier":"Air Canada","operating_carrier":"Air Canada",
        "cabin":"Business"}}]}}]}}
        </script></main></body></html>""",
        encoding="utf-8",
    )

    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(fixture)
    ).execute(criteria, "other-flight-number-price")

    assert result["status"] == "UNVERIFIED_PRICE"


def test_exact_price_requires_visible_association_with_payload_itinerary(criteria, tmp_path):
    fixture = tmp_path / "unassociated-price.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <article><div>60,000 pts + $82.40 CAD</div></article>
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
    ).execute(criteria, "unassociated-price")

    assert result["status"] == "UNVERIFIED_PRICE"


def test_malformed_scalar_warnings_return_parser_failed(criteria, tmp_path):
    fixture = tmp_path / "scalar-warnings.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <article><div>AC 872</div><div>60,000 pts + $82.40 CAD</div></article>
        <script id="aeroplan-results-data" type="application/json">
        {"itineraries":[{"visible":true,"complete_itinerary":true,
        "price_kind":"exact","price_label":"60,000 pts","points_per_passenger":60000,
        "cash":{"displayed_total":"$82.40","currency":"CAD"},"warnings":1,
        "segments":[{"departure":"2026-11-05T20:30:00-05:00",
        "arrival":"2026-11-06T08:35:00+01:00","flight_number":"AC 872",
        "marketing_carrier":"Air Canada","operating_carrier":"Air Canada",
        "cabin":"Business"}]}]}
        </script></main></body></html>""",
        encoding="utf-8",
    )

    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(fixture)
    ).execute(criteria, "scalar-warnings")

    assert result["status"] == "PARSER_FAILED"


def test_visible_from_price_is_rejected_when_payload_claims_exact(criteria, tmp_path):
    fixture = tmp_path / "inconsistent-from-price.html"
    fixture.write_text(
        """<!doctype html><html><body><main data-page-kind="results">
        <div>From 60,000 pts + $82.40 CAD</div>
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
    ).execute(criteria, "inconsistent-from-price")

    assert result["status"] == "UNVERIFIED_PRICE"


@pytest.mark.parametrize("qualified_label", ["From:", "Starting at:"])
def test_visible_punctuated_qualified_price_is_rejected_when_payload_claims_exact(
    criteria, tmp_path, qualified_label
):
    fixture = tmp_path / "punctuated-qualified-price.html"
    fixture.write_text(
        f"""<!doctype html><html><body><main data-page-kind="results">
        <div>{qualified_label} 60,000 pts + $82.40 CAD</div>
        <script id="aeroplan-results-data" type="application/json">
        {{"itineraries":[{{"visible":true,"complete_itinerary":true,
        "price_kind":"exact","price_label":"60,000 pts","points_per_passenger":60000,
        "cash":{{"displayed_total":"$82.40","currency":"CAD"}},
        "segments":[{{"departure":"2026-11-05T20:30:00-05:00",
        "arrival":"2026-11-06T08:35:00+01:00","flight_number":"AC 872",
        "marketing_carrier":"Air Canada","operating_carrier":"Air Canada",
        "cabin":"Business"}}]}}]}}
        </script></main></body></html>""",
        encoding="utf-8",
    )

    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(fixture)
    ).execute(criteria, "punctuated-qualified-price")

    assert result["status"] == "UNVERIFIED_PRICE"


@pytest.mark.parametrize(
    "fixture_name",
    [
        "unverified_estimated_price.html",
        "unverified_suffix_price.html",
        "unverified_upwards_price.html",
        "unverified_footnote_price.html",
    ],
)
def test_visible_inferred_price_forms_are_rejected_when_payload_claims_exact(
    criteria, fixture_name
):
    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(FIXTURES / fixture_name)
    ).execute(criteria, "inferred-price")

    assert result["status"] == "UNVERIFIED_PRICE"


@pytest.mark.parametrize("visible_price", ["At least 60,000 pts", "60,000 pts+"])
def test_visible_minimum_price_forms_are_rejected_when_payload_claims_exact(
    criteria, tmp_path, visible_price
):
    fixture = tmp_path / "minimum-price.html"
    fixture.write_text(
        f"""<!doctype html><html><body><main data-page-kind="results">
        <div>{visible_price} + $82.40 CAD</div>
        <script id="aeroplan-results-data" type="application/json">
        {{"itineraries":[{{"visible":true,"complete_itinerary":true,
        "price_kind":"exact","price_label":"60,000 pts","points_per_passenger":60000,
        "cash":{{"displayed_total":"$82.40","currency":"CAD"}},
        "segments":[{{"departure":"2026-11-05T20:30:00-05:00",
        "arrival":"2026-11-06T08:35:00+01:00","flight_number":"AC 872",
        "marketing_carrier":"Air Canada","operating_carrier":"Air Canada",
        "cabin":"Business"}}]}}]}}
        </script></main></body></html>""",
        encoding="utf-8",
    )

    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(fixture)
    ).execute(criteria, "minimum-price")

    assert result["status"] == "UNVERIFIED_PRICE"


@pytest.mark.parametrize(
    "visible_price",
    ["Around 60,000 pts", "Roughly 60,000 pts", "Circa 60,000 pts"],
)
def test_visible_ambiguous_price_forms_are_rejected_when_payload_claims_exact(
    criteria, tmp_path, visible_price
):
    fixture = tmp_path / "ambiguous-price.html"
    fixture.write_text(
        f"""<!doctype html><html><body><main data-page-kind="results">
        <div>{visible_price} + $82.40 CAD</div>
        <script id="aeroplan-results-data" type="application/json">
        {{"itineraries":[{{"visible":true,"complete_itinerary":true,
        "price_kind":"exact","price_label":"60,000 pts","points_per_passenger":60000,
        "cash":{{"displayed_total":"$82.40","currency":"CAD"}},
        "segments":[{{"departure":"2026-11-05T20:30:00-05:00",
        "arrival":"2026-11-06T08:35:00+01:00","flight_number":"AC 872",
        "marketing_carrier":"Air Canada","operating_carrier":"Air Canada",
        "cabin":"Business"}}]}}]}}
        </script></main></body></html>""",
        encoding="utf-8",
    )

    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(fixture)
    ).execute(criteria, "ambiguous-price")

    assert result["status"] == "UNVERIFIED_PRICE"


@pytest.mark.parametrize(
    "visible_price",
    [
        "60,000 pts estimated + $82.40 CAD",
        "60,000 pts approximately + $82.40 CAD",
        "60,000 pts minimum + $82.40 CAD",
    ],
)
def test_visible_suffix_inferred_price_markers_are_rejected_when_payload_claims_exact(
    criteria, tmp_path, visible_price
):
    fixture = tmp_path / "suffix-inferred-price.html"
    fixture.write_text(
        f"""<!doctype html><html><body><main data-page-kind="results">
        <div>{visible_price}</div>
        <script id="aeroplan-results-data" type="application/json">
        {{"itineraries":[{{"visible":true,"complete_itinerary":true,
        "price_kind":"exact","price_label":"60,000 pts","points_per_passenger":60000,
        "cash":{{"displayed_total":"$82.40","currency":"CAD"}},
        "segments":[{{"departure":"2026-11-05T20:30:00-05:00",
        "arrival":"2026-11-06T08:35:00+01:00","flight_number":"AC 872",
        "marketing_carrier":"Air Canada","operating_carrier":"Air Canada",
        "cabin":"Business"}}]}}]}}
        </script></main></body></html>""",
        encoding="utf-8",
    )

    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(fixture)
    ).execute(criteria, "suffix-inferred-price")

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


@pytest.mark.parametrize(
    "action",
    [
        {"action": "read_results", "Action": "execute_script"},
        {
            "action": "fill_search_field",
            "field": "origin",
            "FIELD": "password",
            "value": "JFK",
        },
        {"action": "read_results", " action ": "execute_script"},
        {"Action": "read_results"},
    ],
)
def test_search_policy_rejects_noncanonical_and_case_colliding_action_keys(action):
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
        "https://www.aircanada.com/search/checkout",
        "https://www.aircanada.com/search/book",
        "https://www.aircanada.com/aeroplan/redeem/availability/payment",
    ],
)
def test_search_policy_rejects_mutation_and_booking_descendants(url):
    with pytest.raises(PolicyViolation):
        SearchPolicy().validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.aircanada.com/search/../checkout",
        "https://www.aircanada.com/aeroplan/redeem/availability/../../../checkout",
        "https://www.aircanada.com/search/%2e%2e/checkout",
        "https://www.aircanada.com/aeroplan/redeem/availability/%2E%2E/%2e%2e/checkout",
        "https://www.aircanada.com/search/%252e%252e/checkout",
    ],
)
def test_search_policy_rejects_dot_segment_path_traversal(url):
    with pytest.raises(PolicyViolation):
        SearchPolicy().validate_url(url)


def test_search_policy_rejects_browser_normalized_backslash_traversal():
    with pytest.raises(PolicyViolation):
        SearchPolicy().validate_url("https://www.aircanada.com/search/..\\checkout")


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


@pytest.mark.parametrize(
    "url",
    [
        "https://login.aircanada.com/account/delete",
        "https://aircanada.b2clogin.com/password-reset",
    ],
)
def test_search_policy_rejects_non_authentication_identity_paths(url):
    with pytest.raises(PolicyViolation):
        SearchPolicy().validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://login.aircanada.com/login",
        "https://aircanada.b2clogin.com/aircanada.onmicrosoft.com/B2C_1A_signin/oauth2/v2.0/authorize",
    ],
)
def test_search_policy_allows_required_identity_authentication_paths(url):
    SearchPolicy().validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://aircanada.b2clogin.com/evil.example/B2C_1A_signin/oauth2/v2.0/authorize",
        "https://aircanada.b2clogin.com/aircanada.onmicrosoft.com/B2C_1A_passwordreset/oauth2/v2.0/authorize",
        "https://aircanada.b2clogin.com/aircanada.onmicrosoft.com/B2C_1A_signin/oauth2/v2.0/authorize?p=B2C_1A_passwordreset",
        "https://aircanada.b2clogin.com/aircanada.onmicrosoft.com/B2C_1A_signin/oauth2/v2.0/authorize?redirect_uri=https%3A%2F%2Fevil.example%2Fcallback",
    ],
)
def test_search_policy_rejects_non_signin_b2c_authorization_flows(url):
    with pytest.raises(PolicyViolation):
        SearchPolicy().validate_url(url)


def test_search_policy_allows_safe_b2c_authorization_parameters():
    SearchPolicy().validate_url(
        "https://aircanada.b2clogin.com/aircanada.onmicrosoft.com/"
        "B2C_1A_signin/oauth2/v2.0/authorize?client_id=fixture-client&"
        "redirect_uri=https%3A%2F%2Fwww.aircanada.com%2Faeroplan%2Fredeem%2Favailability&"
        "response_type=code&scope=openid&state=fixture-state&nonce=fixture-nonce"
    )


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
        BrowserAgentOutcome(
            "completed",
            None,
            1,
            ("https://login.aircanada.com/account/delete",),
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


@pytest.mark.parametrize(
    "blocked_action",
    [
        {"action": "navigate", "url": "https://www.aircanada.com/checkout"},
        {"action": "book_itinerary"},
    ],
)
def test_browser_agent_policy_is_enforced_before_an_action_can_execute(
    criteria, blocked_action
):
    class PolicyAwareBrowser(AeroplanFixtureBrowser):
        def __init__(self):
            super().__init__(
                FIXTURES / "match.html",
                initial_fixture=FIXTURES / "unknown.html",
                deterministic_supported=False,
            )
            self.executed_actions = []

        def browser_agent_search(
            self,
            criteria,
            *,
            max_steps,
            allowed_domains,
            authorize_action,
        ):
            del criteria, max_steps, allowed_domains
            authorize_action(blocked_action)
            self.executed_actions.append(blocked_action)
            raise AssertionError("a rejected action must not execute")

    browser = PolicyAwareBrowser()
    result = AeroplanSearchAdapter(browser=browser).execute(criteria, "boundary-policy")

    assert result["status"] == "BROWSER_AGENT_FAILED"
    assert browser.executed_actions == []


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


@pytest.mark.parametrize(
    "hidden_attribute",
    [
        "hidden",
        'aria-hidden="true"',
        'style="display: none"',
        'style="visibility:hidden"',
    ],
)
def test_prices_inside_hidden_dom_containers_do_not_qualify(
    criteria, tmp_path, hidden_attribute
):
    fixture = tmp_path / "hidden-price.html"
    fixture.write_text(
        f"""<!doctype html><html><body><main data-page-kind="results">
        <div {hidden_attribute}><span>60,000 pts + $82.40 CAD</span></div>
        <script id="aeroplan-results-data" type="application/json">
        {{"itineraries":[{{"visible":true,"complete_itinerary":true,
        "price_kind":"exact","price_label":"60,000 pts","points_per_passenger":60000,
        "cash":{{"displayed_total":"$82.40","currency":"CAD"}},
        "segments":[{{"departure":"2026-11-05T20:30:00-05:00",
        "arrival":"2026-11-06T08:35:00+01:00","flight_number":"AC 872",
        "marketing_carrier":"Air Canada","operating_carrier":"Air Canada",
        "cabin":"Business"}}]}}]}}
        </script></main></body></html>""",
        encoding="utf-8",
    )

    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(fixture)
    ).execute(criteria, "hidden-price")

    assert result["status"] == "UNVERIFIED_PRICE"


def test_price_hidden_by_stylesheet_class_does_not_qualify(criteria, tmp_path):
    fixture = tmp_path / "css-hidden-price.html"
    fixture.write_text(
        """<!doctype html><html><head>
        <style>.hidden-price { display: none; }</style>
        </head><body><main data-page-kind="results">
        <div class="hidden-price"><span>60,000 pts + $82.40 CAD</span></div>
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
    ).execute(criteria, "css-hidden-price")

    assert result["status"] == "UNVERIFIED_PRICE"


@pytest.mark.parametrize(
    "style_rule, price_attribute",
    [
        (".card .price { display: none; }", 'class="price"'),
        (".card > .price { visibility: hidden; }", 'class="price"'),
        (".card span.price { opacity: 0; }", 'class="price"'),
        ("", 'style="opacity: 0"'),
    ],
)
def test_price_hidden_by_computed_style_does_not_qualify(
    criteria, tmp_path, style_rule, price_attribute
):
    fixture = tmp_path / "computed-hidden-price.html"
    fixture.write_text(
        f"""<!doctype html><html><head><style>{style_rule}</style></head><body>
        <main data-page-kind="results"><div class="card">
        <span {price_attribute}>60,000 pts + $82.40 CAD</span></div>
        <script id="aeroplan-results-data" type="application/json">
        {{"itineraries":[{{"visible":true,"complete_itinerary":true,
        "price_kind":"exact","price_label":"60,000 pts","points_per_passenger":60000,
        "cash":{{"displayed_total":"$82.40","currency":"CAD"}},
        "segments":[{{"departure":"2026-11-05T20:30:00-05:00",
        "arrival":"2026-11-06T08:35:00+01:00","flight_number":"AC 872",
        "marketing_carrier":"Air Canada","operating_carrier":"Air Canada",
        "cabin":"Business"}}]}}]}}
        </script></main></body></html>""",
        encoding="utf-8",
    )

    result = AeroplanSearchAdapter(
        browser=AeroplanFixtureBrowser(fixture)
    ).execute(criteria, "computed-hidden-price")

    assert result["status"] == "UNVERIFIED_PRICE"


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
