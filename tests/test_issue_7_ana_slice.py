import json
import os
from datetime import date
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

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


def test_award_page_header_login_link_does_not_require_authentication(criteria):
    from flight_search_demo.ana import AnaFixtureBrowser, AnaSearchAdapter

    browser = AnaFixtureBrowser(
        FIXTURES / "match.html",
        initial_fixture=FIXTURES / "award_page_with_header_login.html",
        deterministic_supported=False,
    )

    result = AnaSearchAdapter(browser=browser).execute(criteria, "ana-header-login")

    assert result["status"] == "MATCH_FOUND"
    assert result["browser_agent_used"] is True


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


def test_search_policy_confines_ana_award_flow_paths_and_query_parameters():
    from flight_search_demo.ana import (
        AnaSearchPolicy, OFFICIAL_ANA_AWARD_FLOW_TRANSITION_URL, PolicyViolation,
    )

    policy = AnaSearchPolicy()
    policy.validate_url(OFFICIAL_ANA_AWARD_FLOW_TRANSITION_URL)
    policy.validate_url(
        "https://aswbe-i.ana.co.jp/international_asw/pages/award/search/roundtrip/"
        "award_search_roundtrip_input.xhtml?CONNECTION_KIND=LAX&LANG=en"
    )

    for url in (
        "https://aswbe-i.ana.co.jp/international_asw/account/delete?confirm=yes",
        "https://aswbe-i.ana.co.jp/international_asw/pages/award/search/roundtrip/"
        "award_search_roundtrip_input.xhtml?CONNECTION_KIND=LAX&LANG=en&confirm=yes",
        "https://aswbe-i.ana.co.jp/international_asw/pages/award/search/roundtrip/"
        "award_search_roundtrip_input.xhtml?CONNECTION_KIND=JPN&LANG=en",
        "https://aswbe-i.ana.co.jp/international_asw/pages/award/search/roundtrip/"
        "award_search_roundtrip_input.xhtml?CONNECTION_KIND=LAX&LANG=ja",
        "https://www.ana.co.jp/other/int/meta/0771.html?CONNECTION_KIND=us&LANG=en",
        "https://www.ana.co.jp/other/int/meta/0771.html?CONNECTION_KIND=us&LANG=e&retry=1",
    ):
        with pytest.raises(PolicyViolation):
            policy.validate_url(url)


def test_search_policy_allows_only_one_cta_transition_before_search_interaction(criteria):
    from flight_search_demo.ana import (
        AnaSearchPolicy, OFFICIAL_ANA_AWARD_FLOW_TRANSITION_URL, PolicyViolation,
    )

    policy = AnaSearchPolicy()
    policy.validate_criteria(criteria)
    transition = {
        "action": "enter_award_flow",
        "url": OFFICIAL_ANA_AWARD_FLOW_TRANSITION_URL,
    }
    policy.validate_action(transition)
    with pytest.raises(PolicyViolation, match="at most once"):
        policy.validate_action(transition)

    policy.validate_criteria(criteria)
    with pytest.raises(PolicyViolation, match="approved CTA"):
        policy.validate_action({
            "action": "navigate", "url": OFFICIAL_ANA_AWARD_FLOW_TRANSITION_URL,
        })
    with pytest.raises(PolicyViolation, match="award-flow transition"):
        policy.validate_action({"action": "fill_search_field", "field": "origin", "value": "SFO"})
    with pytest.raises(PolicyViolation, match="award-flow transition"):
        policy.validate_action({"action": "submit_search"})


def test_search_policy_binds_actions_to_confirmed_values_and_one_submission(criteria):
    from flight_search_demo.ana import (
        AnaSearchPolicy, OFFICIAL_ANA_AWARD_FLOW_TRANSITION_URL, PolicyViolation,
    )

    policy = AnaSearchPolicy()
    policy.validate_criteria(criteria)
    policy.validate_action({
        "action": "enter_award_flow",
        "url": OFFICIAL_ANA_AWARD_FLOW_TRANSITION_URL,
    })
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
    assert driver.debugger_port == 9224
    assert driver.profile_path == DEFAULT_ANA_PROFILE
    assert "ana-browser-profile" in str(driver.profile_path)


def test_live_driver_discovers_the_dedicated_ana_debugger_endpoint(tmp_path, monkeypatch):
    from flight_search_demo import ana_validation

    monkeypatch.delenv("STEEL_API_KEY", raising=False)
    driver = ana_validation.PersistentAnaDriver(profile_path=tmp_path / "ana.json")

    with patch.object(
        ana_validation,
        "discover_debugger_cdp_url",
        side_effect=RuntimeError("endpoint unavailable"),
    ) as discover:
        result = driver.execute(object(), deadline_seconds=30, reserve_submission=lambda: True)

    assert result["status"] == "MANUAL_SEARCH_ONLY"
    assert discover.call_args.args[:2] == ("http://127.0.0.1:3001", None)
    assert discover.call_args.kwargs["debugger_port"] == 9224


def test_dedicated_ana_debugger_discovery_does_not_resolve_to_aeroplan_port():
    from flight_search_demo.spike import discover_debugger_cdp_url

    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.read.return_value = (
        b'{"webSocketDebuggerUrl":"ws://127.0.0.1/devtools/browser/ana"}'
    )

    with patch("flight_search_demo.spike.urllib.request.urlopen", return_value=response) as open_url:
        cdp_url = discover_debugger_cdp_url(
            "http://127.0.0.1:3001", None, debugger_port=9224
        )

    open_url.assert_called_once_with(
        "http://127.0.0.1:9224/json/version", timeout=10.0
    )
    assert cdp_url == "ws://127.0.0.1:9224/devtools/browser/ana"


def test_live_cli_fails_closed_in_ci_before_constructing_driver(tmp_path, monkeypatch):
    from flight_search_demo import ana_validation

    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(
        "sys.argv",
        [
            "ana-validation",
            "--request", "request.json",
            "--confirmation", "confirmation.json",
            "--event-log", str(tmp_path / "events.jsonl"),
            "--acknowledge-account-and-terms-risk",
        ],
    )

    with patch.object(ana_validation, "PersistentAnaDriver") as driver:
        with pytest.raises(SystemExit):
            ana_validation.main()

    driver.assert_not_called()


def test_controlled_validation_fails_closed_in_ci_before_executing_supplied_driver(
    tmp_path, monkeypatch,
):
    from flight_search_demo.ana_validation import validate_controlled_ana_search

    request, confirmation = _request_and_confirmation()
    monkeypatch.setenv("CI", "true")

    class Driver:
        def execute(self, criteria, *, deadline_seconds, reserve_submission):
            raise AssertionError("live ANA driver must not execute in CI")

    event = validate_controlled_ana_search(
        request=request,
        confirmation=confirmation,
        risk_acknowledged=True,
        event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3",
        current_date=date(2026, 9, 12),
        live_driver=Driver(),
    )

    assert event["status"] == "MANUAL_SEARCH_ONLY"
    assert event["blocking_reason"] == "LIVE_SEARCH_DISABLED_IN_CI"
    assert event["live_validation_performed"] is False
    assert event["live_search_submitted"] is False
    assert event["allowance_remaining"] == 10


def test_persistent_ana_profile_rejects_non_ana_state_before_browser_start(tmp_path):
    from flight_search_demo.ana_validation import PersistentAnaBrowser

    profile = tmp_path / "ana-profile" / "storage-state.json"
    profile.parent.mkdir()
    profile.write_text(json.dumps({
        "cookies": [
            {"name": "ana", "value": "ok", "domain": ".ana.co.jp", "path": "/"},
            {"name": "air-canada", "value": "mixed", "domain": ".aircanada.com", "path": "/"},
        ],
        "origins": [],
    }))
    browser = PersistentAnaBrowser(
        cdp_url="ws://127.0.0.1/devtools/browser/test",
        profile_path=profile,
        deadline_seconds=30,
        reserve_submission=lambda: True,
    )

    with patch("flight_search_demo.ana_validation.BrowserSession") as session:
        with pytest.raises(ValueError, match="non-ANA"):
            browser.__enter__()

    session.assert_not_called()


def test_persistent_ana_profile_artifacts_are_owner_only(tmp_path):
    from flight_search_demo.ana_validation import PersistentAnaBrowser

    profile = tmp_path / "ana-profile" / "storage-state.json"
    profile.parent.mkdir(mode=0o775)
    profile.write_text('{"cookies": [], "origins": []}')
    profile.chmod(0o664)
    backup = profile.with_suffix(profile.suffix + ".bak")
    backup.write_text('{"cookies": [], "origins": []}')
    backup.chmod(0o664)
    browser = PersistentAnaBrowser(
        cdp_url="ws://127.0.0.1/devtools/browser/test",
        profile_path=profile,
        deadline_seconds=30,
        reserve_submission=lambda: True,
    )

    class Session:
        async def start(self):
            return None

        async def stop(self):
            profile.chmod(0o664)

    with patch("flight_search_demo.ana_validation.BrowserSession", return_value=Session()):
        with browser:
            assert os.stat(profile.parent).st_mode & 0o777 == 0o700
            assert os.stat(profile).st_mode & 0o777 == 0o600
            assert os.stat(backup).st_mode & 0o777 == 0o600

    assert os.stat(profile.parent).st_mode & 0o777 == 0o700
    assert os.stat(profile).st_mode & 0o777 == 0o600
    assert os.stat(backup).st_mode & 0o777 == 0o600


def test_persistent_ana_profile_rejects_non_ana_state_written_on_stop(tmp_path):
    from flight_search_demo.ana_validation import PersistentAnaBrowser

    profile = tmp_path / "ana-profile" / "storage-state.json"
    browser = PersistentAnaBrowser(
        cdp_url="ws://127.0.0.1/devtools/browser/test",
        profile_path=profile,
        deadline_seconds=30,
        reserve_submission=lambda: True,
    )

    class Session:
        async def start(self):
            return None

        async def stop(self):
            profile.write_text(json.dumps({
                "cookies": [{
                    "name": "mixed", "value": "state",
                    "domain": ".aircanada.com", "path": "/",
                }],
                "origins": [],
            }))

    with patch("flight_search_demo.ana_validation.BrowserSession", return_value=Session()):
        with pytest.raises(ValueError, match="non-ANA"):
            with browser:
                pass

    assert os.stat(profile).st_mode & 0o777 == 0o600


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
                0: Node(
                    tag_name="a",
                    href="/other/int/meta/0771.html?CONNECTION_KIND=us&LANG=e",
                    text="Reserve/(Purchase) Apply",
                ),
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
    captured = []

    class Agent:
        def __init__(self, **kwargs):
            self.task = kwargs["task"]
            captured.append(kwargs)
            self.multi_act = self._multi_act

        async def _multi_act(self, actions):
            for action in actions:
                payload = action.model_dump(exclude_none=True)
                name = next(iter(payload))
                executions.append((name, list(reservations)))
            return []

        async def run(self, max_steps):
            if "enter the official award flow" in self.task:
                await self.multi_act([SimpleNamespace(model_dump=lambda **kwargs: {
                    "click": {"index": 0}
                })])
                steps = 1
            elif "fill only the confirmed search criteria" in self.task:
                actions = (
                    ("input", {"index": 1, "text": "SFO"}),
                    ("input", {"index": 2, "text": "NRT"}),
                    ("input", {"index": 3, "text": "2026-09-17"}),
                    ("select_dropdown", {"index": 4, "text": "Business"}),
                    ("select_dropdown", {"index": 5, "text": "1"}),
                    ("select_dropdown", {"index": 6, "text": "one-way"}),
                )
                for offset in range(0, len(actions), 2):
                    batch = [SimpleNamespace(
                        model_dump=lambda name=name, params=params, **kwargs: {name: params}
                    ) for name, params in actions[offset:offset + 2]]
                    await self.multi_act(batch)
                steps = 3
            elif "submit the confirmed criteria" in self.task:
                await self.multi_act([SimpleNamespace(model_dump=lambda **kwargs: {
                    "click": {"index": 7}
                })])
                steps = 1
            else:
                steps = 1
            return SimpleNamespace(
                is_done=lambda: True, is_successful=lambda: True,
                number_of_steps=lambda: steps, errors=lambda: [],
                urls=lambda: [OFFICIAL_ANA_SEARCH_ENTRY_URL],
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
    assert len(captured) == 4
    assert all(item["max_actions_per_step"] == 2 for item in captured)
    assert all(item["max_failures"] == 2 for item in captured)
    assert all("SFO" in item["task"] and "NRT" in item["task"] for item in captured)


def test_persistent_browser_uses_separate_state_specific_agent_goals(criteria):
    from types import SimpleNamespace
    from flight_search_demo.ana import AnaSearchPolicy, OFFICIAL_ANA_SEARCH_ENTRY_URL
    from flight_search_demo.ana_validation import PersistentAnaBrowser

    tasks = []

    class Agent:
        def __init__(self, **kwargs):
            tasks.append(kwargs["task"])
            self.multi_act = self._multi_act

        async def _multi_act(self, actions):
            del actions
            return []

        async def run(self, max_steps):
            del max_steps
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
    browser._snapshot = lambda: __import__(
        "flight_search_demo.aeroplan", fromlist=["PageSnapshot"]
    ).PageSnapshot(OFFICIAL_ANA_SEARCH_ENTRY_URL, "<html><body>ANA</body></html>")
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
                criteria, goal="confirmed criteria only", max_steps=8,
                max_actions_per_step=2, max_failures=2,
                allowed_domains=sorted(policy.allowed_domains),
                authorize_action=policy.validate_action,
            )
    finally:
        browser._loop.close()

    assert outcome.status == "completed"
    assert len(tasks) == 4
    assert "enter the official award flow" in tasks[0]
    assert "fill only the confirmed search criteria" in tasks[1]
    assert "submit the confirmed criteria exactly once" in tasks[2]
    assert "read the visible final ANA availability state" in tasks[3]


@pytest.mark.parametrize("redirect_after", [1, 2])
def test_persistent_browser_rejects_redirect_before_next_agent_phase(
    criteria, redirect_after,
):
    from types import SimpleNamespace
    from flight_search_demo.ana import AnaSearchPolicy, OFFICIAL_ANA_SEARCH_ENTRY_URL
    from flight_search_demo.ana_validation import PersistentAnaBrowser

    executed_phases = []
    completed_phases = 0

    class Agent:
        def __init__(self, **kwargs):
            self.task = kwargs["task"]
            self.multi_act = self._multi_act

        async def _multi_act(self, actions):
            del actions
            return []

        async def run(self, max_steps):
            nonlocal completed_phases
            del max_steps
            executed_phases.append(self.task)
            completed_phases += 1
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
    browser._snapshot = lambda: __import__(
        "flight_search_demo.aeroplan", fromlist=["PageSnapshot"]
    ).PageSnapshot(
        "https://www.ana.co.jp/en/us/shopping/"
        if completed_phases == redirect_after else OFFICIAL_ANA_SEARCH_ENTRY_URL,
        "<html><body>redirected</body></html>",
    )
    policy = AnaSearchPolicy()
    policy.validate_criteria(criteria)
    try:
        with patch("flight_search_demo.ana_validation.Agent", Agent), patch(
            "flight_search_demo.ana_validation.Controller"
        ) as controller, patch(
            "flight_search_demo.ana_validation.build_agent_llms",
            return_value=(object(), object()),
        ), pytest.raises(ValueError, match="expected ANA page"):
            controller.return_value.registry.registry.actions = {}
            browser.browser_agent_search(
                criteria, goal="confirmed criteria only", max_steps=8,
                max_actions_per_step=2, max_failures=2,
                allowed_domains=sorted(policy.allowed_domains),
                authorize_action=policy.validate_action,
            )
    finally:
        browser._loop.close()

    assert len(executed_phases) == redirect_after


def test_persistent_browser_authorizes_real_entry_page_award_flow_transition_before_form_input(
    criteria,
):
    from types import SimpleNamespace
    from flight_search_demo.ana import AnaSearchPolicy
    from flight_search_demo.ana_validation import PersistentAnaBrowser

    entry_html = (FIXTURES / "official_entry.html").read_text()
    assert "/other/int/meta/0771.html?CONNECTION_KIND=us&amp;LANG=e" in entry_html

    class Node:
        def __init__(self, *, tag_name="input", text="", **attributes):
            self.attributes = attributes
            self.tag_name = tag_name
            self._text = text

        def get_all_children_text(self):
            return self._text

    class Session:
        async def get_element_by_index(self, index):
            return {
                1: Node(
                    tag_name="a",
                    text="Reserve/(Purchase) Apply",
                    href="/other/int/meta/0771.html?CONNECTION_KIND=us&LANG=e",
                ),
                2: Node(name="origin"),
            }[index]

        async def get_current_page(self):
            return SimpleNamespace(evaluate=lambda script: _async_value(
                "https://www.ana.co.jp/other/int/meta/0771.html?CONNECTION_KIND=us&LANG=e"
                if "location.href" in script else entry_html
            ))

    async def _async_value(value):
        return value

    class Agent:
        def __init__(self, **kwargs):
            self.task = kwargs["task"]
            self.multi_act = self._multi_act

        async def _multi_act(self, actions):
            return []

        async def run(self, max_steps):
            del max_steps
            if "enter the official award flow" in self.task:
                await self.multi_act([SimpleNamespace(model_dump=lambda **kwargs: {
                    "click": {"index": 1}
                })])
            elif "fill only the confirmed search criteria" in self.task:
                await self.multi_act([SimpleNamespace(model_dump=lambda **kwargs: {
                    "input": {"index": 2, "text": "SFO"}
                })])
            return SimpleNamespace(
                is_done=lambda: True, is_successful=lambda: True,
                number_of_steps=lambda: 1, errors=lambda: [],
                urls=lambda: [
                    "https://www.ana.co.jp/en/us/amc/international-flight-awards/",
                    "https://www.ana.co.jp/other/int/meta/0771.html?CONNECTION_KIND=us&LANG=e",
                ],
            )

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
        ):
            outcome = browser.browser_agent_search(
                criteria, goal="enter official award flow", max_steps=4,
                max_actions_per_step=1, max_failures=1,
                allowed_domains=sorted(policy.allowed_domains),
                authorize_action=policy.validate_action,
            )
    finally:
        browser._loop.close()

    assert outcome.status == "completed"
    assert outcome.actions == (
        {
            "action": "enter_award_flow",
            "url": "https://www.ana.co.jp/other/int/meta/0771.html?CONNECTION_KIND=us&LANG=e",
        },
        {"action": "fill_search_field", "field": "origin", "value": "SFO"},
    )


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
            self.task = kwargs["task"]
            self.multi_act = self._multi_act

        async def _multi_act(self, actions):
            payload = actions[0].model_dump(exclude_none=True)
            if "extract" in payload:
                return [SimpleNamespace(extracted_content=json.dumps(extraction))]
            return [SimpleNamespace(extracted_content=None)]

        async def run(self, max_steps):
            del max_steps
            if "read the visible final ANA availability state" in self.task:
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
        ("<html><body><article>NH 107 SFO to NRT Business "
         "75,000 miles + $43.10 USD</article></body></html>"),
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


def test_browser_agent_result_evidence_must_be_visible_in_final_dom(criteria):
    from flight_search_demo.aeroplan import PageSnapshot, UnsupportedDeterministicLayout
    from flight_search_demo.ana import (
        AnaAgentOutcome, AnaSearchAdapter, OFFICIAL_ANA_SEARCH_ENTRY_URL,
    )

    extraction = {
        "page_kind": "results",
        "visible_text": ["NH 107 Business 75,000 miles + $43.10 USD"],
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
    final_page = PageSnapshot(
        OFFICIAL_ANA_SEARCH_ENTRY_URL,
        "<html><body><main>ANA Mileage Club</main></body></html>",
    )

    class Browser:
        def open(self, url):
            return PageSnapshot(url, "<html><body>ANA award search</body></html>")

        def deterministic_search(self, criteria):
            raise UnsupportedDeterministicLayout

        def browser_agent_search(self, criteria, **kwargs):
            return AnaAgentOutcome(
                status="completed", page=final_page, steps=1, failures=0,
                visited_urls=(final_page.url,), actions=(), extraction=extraction,
            )

    result = AnaSearchAdapter(browser=Browser()).execute(criteria, "dom-evidence")

    assert result["status"] == "UNVERIFIED_PRICE"


def test_browser_agent_no_availability_must_be_visible_in_final_dom(criteria):
    from flight_search_demo.aeroplan import PageSnapshot, UnsupportedDeterministicLayout
    from flight_search_demo.ana import (
        AnaAgentOutcome, AnaSearchAdapter, OFFICIAL_ANA_SEARCH_ENTRY_URL,
    )

    final_page = PageSnapshot(
        OFFICIAL_ANA_SEARCH_ENTRY_URL,
        "<html><body><main>ANA Mileage Club</main></body></html>",
    )

    class Browser:
        def open(self, url):
            return PageSnapshot(url, "<html><body>ANA award search</body></html>")

        def deterministic_search(self, criteria):
            raise UnsupportedDeterministicLayout

        def browser_agent_search(self, criteria, **kwargs):
            return AnaAgentOutcome(
                status="completed", page=final_page, steps=1, failures=0,
                visited_urls=(final_page.url,), actions=(),
                extraction={"page_kind": "no_availability"},
            )

    result = AnaSearchAdapter(browser=Browser()).execute(criteria, "dom-no-availability")

    assert result["status"] == "VALIDATION_REJECTED"


def test_persistent_browser_rejects_actions_before_agent_dispatch(criteria):
    from types import SimpleNamespace
    from flight_search_demo.aeroplan import PageSnapshot
    from flight_search_demo.ana import AnaSearchPolicy, OFFICIAL_ANA_SEARCH_ENTRY_URL
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
    browser._snapshot = lambda: PageSnapshot(
        OFFICIAL_ANA_SEARCH_ENTRY_URL, "<html><body>ANA award search</body></html>"
    )
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


def test_controlled_validation_reports_unexpected_live_driver_failure(tmp_path):
    from flight_search_demo.ana_validation import validate_controlled_ana_search

    request, confirmation = _request_and_confirmation()

    class Driver:
        def execute(self, criteria, *, deadline_seconds, reserve_submission):
            del criteria, deadline_seconds, reserve_submission
            raise RuntimeError("browser process exited")

    event = validate_controlled_ana_search(
        request=request,
        confirmation=confirmation,
        risk_acknowledged=True,
        event_log_path=tmp_path / "events.jsonl",
        allowance_db_path=tmp_path / "usage.sqlite3",
        current_date=date(2026, 9, 12),
        live_driver=Driver(),
    )

    assert event["status"] == "MANUAL_SEARCH_ONLY"
    assert event["blocking_reason"] == "LIVE_DRIVER_FAILED"
    assert event["adapter_status"] == "UNVERIFIED"
    assert event["continuous_live_execution_enabled"] is False
    assert "RuntimeError" in event["detail"]
    assert "browser process exited" not in event["detail"]
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
