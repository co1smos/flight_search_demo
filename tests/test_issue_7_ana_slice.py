import json
from datetime import date
from pathlib import Path

import pytest
from unittest.mock import patch

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
    ("field", "wrong_value"),
    [("origin", "LAX"), ("destination", "HND"), ("departure", "2026-09-18T01:20:00-07:00")],
)
def test_itinerary_route_and_date_must_match_confirmed_criteria(
    criteria, tmp_path, field, wrong_value
):
    from flight_search_demo.ana import AnaFixtureBrowser, AnaSearchAdapter

    html = (FIXTURES / "match.html").read_text()
    replacements = {
        "origin": ('"origin":"SFO"', f'"origin":"{wrong_value}"'),
        "destination": ('"destination":"NRT"', f'"destination":"{wrong_value}"'),
        "departure": (
            '"departure":"2026-09-17T01:20:00-07:00"',
            f'"departure":"{wrong_value}"',
        ),
    }
    old, new = replacements[field]
    fixture = tmp_path / "wrong-itinerary.html"
    fixture.write_text(html.replace(old, new))

    result = AnaSearchAdapter(browser=AnaFixtureBrowser(fixture)).execute(criteria, "route-date")
    assert result["status"] == "VALIDATION_REJECTED"


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


def test_search_policy_binds_actions_to_confirmed_values_and_one_submission(criteria):
    from flight_search_demo.ana import AnaSearchPolicy, PolicyViolation

    policy = AnaSearchPolicy()
    policy.validate_criteria(criteria)
    policy.validate_action({"action": "fill_search_field", "field": "origin", "value": "SFO"})
    with pytest.raises(PolicyViolation, match="confirmed criteria"):
        policy.validate_action(
            {"action": "fill_search_field", "field": "departure_date", "value": "2026-09-18"}
        )
    with pytest.raises(PolicyViolation, match="all confirmed criteria"):
        policy.validate_action({"action": "submit_search"})
    for field, value in (
        ("destination", "NRT"),
        ("departure_date", "2026-09-17"),
        ("cabin", "Business"),
        ("adults", "1"),
        ("trip_type", "one-way"),
    ):
        policy.validate_action(
            {"action": "select_search_option", "field": field, "value": value}
        )
    policy.validate_action({"action": "submit_search"})
    with pytest.raises(PolicyViolation, match="at most once"):
        policy.validate_action({"action": "submit_search"})


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


def test_persistent_browser_runs_bounded_agent_and_reserves_at_single_submit(criteria):
    from types import SimpleNamespace
    from flight_search_demo.ana import AnaSearchPolicy, OFFICIAL_ANA_SEARCH_ENTRY_URL
    from flight_search_demo.ana_validation import PersistentAnaBrowser

    class Node:
        def __init__(self, **attributes):
            self.attributes = attributes
            self.tag_name = attributes.pop("tag_name", "input")

        def get_all_children_text(self):
            return self.attributes.get("text", "")

    class Session:
        async def get_element_by_index(self, index):
            return {
                1: Node(name="origin"),
                2: Node(name="destination"),
                3: Node(name="departure_date"),
                4: Node(name="cabin"),
                5: Node(name="adults"),
                6: Node(name="trip-type"),
                7: Node(tag_name="button", id="award-search", text="Search"),
            }[index]

        async def get_current_page(self):
            return SimpleNamespace(
                evaluate=lambda script: _async_value(
                    OFFICIAL_ANA_SEARCH_ENTRY_URL if "location.href" in script
                    else '<main data-page-kind="no_availability">No seats</main>'
                )
            )

    async def _async_value(value):
        return value

    reservations = []
    executions = []
    captured = {}

    class Agent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.multi_act = self._multi_act

        async def _multi_act(self, actions):
            for action in actions:
                payload = action.model_dump(exclude_none=True)
                name = next(iter(payload))
                executions.append((name, list(reservations)))
            return []

        async def run(self, max_steps):
            captured["max_steps"] = max_steps
            actions = (
                ("input", {"index": 1, "text": "SFO"}),
                ("input", {"index": 2, "text": "NRT"}),
                ("input", {"index": 3, "text": "2026-09-17"}),
                ("select_dropdown", {"index": 4, "text": "Business"}),
                ("select_dropdown", {"index": 5, "text": "1"}),
                ("select_dropdown", {"index": 6, "text": "one-way"}),
            )
            for name, params in actions:
                await self.multi_act([SimpleNamespace(
                    model_dump=lambda name=name, params=params, **kwargs: {name: params}
                )])
            await self.multi_act([SimpleNamespace(model_dump=lambda **kwargs: {
                "click": {"index": 7}
            })])
            return SimpleNamespace(
                is_done=lambda: True, is_successful=lambda: True,
                number_of_steps=lambda: 7, errors=lambda: [], urls=lambda: [OFFICIAL_ANA_SEARCH_ENTRY_URL],
            )

    browser = PersistentAnaBrowser(
        cdp_url="ws://127.0.0.1/devtools/browser/test",
        profile_path=Path("unused.json"), deadline_seconds=30,
        reserve_submission=lambda: reservations.append("reserved") or True,
    )
    browser._loop = __import__("asyncio").new_event_loop()
    browser._session = Session()
    policy = AnaSearchPolicy()
    policy.validate_criteria(criteria)
    try:
        with patch("flight_search_demo.ana_validation.Agent", Agent), patch(
            "flight_search_demo.ana_validation.build_agent_llms", return_value=(object(), object())
        ):
            outcome = browser.browser_agent_search(
                criteria, goal="confirmed criteria only", max_steps=8,
                max_actions_per_step=2, max_failures=2,
                allowed_domains=sorted(policy.allowed_domains),
                authorize_action=policy.validate_action,
            )
    finally:
        browser._loop.close()

    assert outcome.status == "completed"
    assert reservations == ["reserved"]
    assert executions[-1] == ("click", ["reserved"])
    assert captured["max_steps"] == 8
    assert captured["max_actions_per_step"] == 2
    assert captured["max_failures"] == 2
    assert "SFO" in captured["task"] and "NRT" in captured["task"]


def test_production_agent_extraction_validates_results_without_fixture_markers(criteria):
    from types import SimpleNamespace
    from flight_search_demo.aeroplan import PageSnapshot, UnsupportedDeterministicLayout
    from flight_search_demo.ana import (
        AnaSearchAdapter, AnaSearchPolicy, OFFICIAL_ANA_SEARCH_ENTRY_URL,
    )
    from flight_search_demo.ana_validation import PersistentAnaBrowser

    extraction = {
        "page_kind": "results",
        "visible_text": [
            "NH 107 SFO to NRT Business 75,000 miles + $43.10 USD"
        ],
        "itineraries": [{
            "visible": True,
            "complete_itinerary": True,
            "price_kind": "exact",
            "price_label": "75,000 miles",
            "points_per_passenger": 75000,
            "cash": {"displayed_total": "$43.10", "currency": "USD"},
            "segments": [{
                "origin": "SFO", "destination": "NRT",
                "departure": "2026-09-17T01:20:00-07:00",
                "arrival": "2026-09-18T04:30:00+09:00",
                "flight_number": "NH 107", "marketing_carrier": "ANA",
                "operating_carrier": "ANA", "cabin": "Business",
            }],
        }],
    }

    class Agent:
        def __init__(self, **kwargs):
            self.multi_act = self._multi_act

        async def _multi_act(self, actions):
            payload = actions[0].model_dump(exclude_none=True)
            if "extract" in payload:
                return [SimpleNamespace(extracted_content=json.dumps(extraction))]
            return [SimpleNamespace(extracted_content=None)]

        async def run(self, max_steps):
            del max_steps
            await self.multi_act([SimpleNamespace(model_dump=lambda **kwargs: {
                "extract": {"query": "ANA award results as JSON", "extract_links": False}
            })])
            return SimpleNamespace(
                is_done=lambda: True, is_successful=lambda: True,
                number_of_steps=lambda: 1, errors=lambda: [],
                urls=lambda: [OFFICIAL_ANA_SEARCH_ENTRY_URL],
            )

    browser = PersistentAnaBrowser(
        cdp_url="ws://127.0.0.1/devtools/browser/test",
        profile_path=Path("unused.json"), deadline_seconds=30,
        reserve_submission=lambda: True,
    )
    browser._loop = __import__("asyncio").new_event_loop()
    browser._session = SimpleNamespace()
    browser._snapshot = lambda: PageSnapshot(
        OFFICIAL_ANA_SEARCH_ENTRY_URL,
        "<html><body><article>ANA award availability</article></body></html>",
    )
    policy = AnaSearchPolicy()
    policy.validate_criteria(criteria)
    try:
        with patch("flight_search_demo.ana_validation.Agent", Agent), patch(
            "flight_search_demo.ana_validation.Controller"
        ) as controller, patch(
            "flight_search_demo.ana_validation.build_agent_llms",
            return_value=(object(), object()),
        ):
            controller.return_value.registry.registry.actions = {}
            outcome = browser.browser_agent_search(
                criteria, goal="read submitted results", max_steps=8,
                max_actions_per_step=2, max_failures=2,
                allowed_domains=sorted(policy.allowed_domains),
                authorize_action=policy.validate_action,
            )
    finally:
        browser._loop.close()

    class ProductionOutcomeBrowser:
        def open(self, url):
            return PageSnapshot(url, "<html><body>ANA award search</body></html>")

        def deterministic_search(self, criteria):
            raise UnsupportedDeterministicLayout

        def browser_agent_search(self, criteria, **kwargs):
            return outcome

    result = AnaSearchAdapter(browser=ProductionOutcomeBrowser()).execute(
        criteria, "production-extraction"
    )

    assert outcome.extraction == extraction
    assert result["status"] == "MATCH_FOUND"
    assert result["itineraries"][0]["segments"][0]["flight_number"] == "NH 107"


def test_persistent_browser_rejects_actions_before_agent_dispatch(criteria):
    from types import SimpleNamespace
    from flight_search_demo.ana import AnaSearchPolicy
    from flight_search_demo.ana_validation import PersistentAnaBrowser

    executions = []

    class Session:
        async def get_element_by_index(self, index):
            return SimpleNamespace(attributes={"name": "origin"}, tag_name="input")

    class Agent:
        def __init__(self, **kwargs):
            self.multi_act = self._multi_act

        async def _multi_act(self, actions):
            executions.extend(actions)
            return []

        async def run(self, max_steps):
            del max_steps
            await self.multi_act([SimpleNamespace(model_dump=lambda **kwargs: {
                "input": {"index": 1, "text": "SFO", "clear": False}
            })])

    browser = PersistentAnaBrowser(
        cdp_url="ws://127.0.0.1/devtools/browser/test",
        profile_path=Path("unused.json"), deadline_seconds=30,
        reserve_submission=lambda: True,
    )
    browser._loop = __import__("asyncio").new_event_loop()
    browser._session = Session()
    policy = AnaSearchPolicy()
    policy.validate_criteria(criteria)
    try:
        with patch("flight_search_demo.ana_validation.Agent", Agent), patch(
            "flight_search_demo.ana_validation.build_agent_llms", return_value=(object(), object())
        ), pytest.raises(ValueError, match="replace, not append"):
            browser.browser_agent_search(
                criteria, goal="confirmed criteria only", max_steps=8,
                max_actions_per_step=2, max_failures=2,
                allowed_domains=sorted(policy.allowed_domains),
                authorize_action=policy.validate_action,
            )
    finally:
        browser._loop.close()

    assert executions == []


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
