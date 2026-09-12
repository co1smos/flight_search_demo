"""Policy-bound ANA Mileage Club award-search slice."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import unquote, urlparse

from .aeroplan import (
    PageSnapshot,
    PolicyViolation,
    SearchTimeout,
    UnsupportedDeterministicLayout,
    _ResultsScriptParser,
    _is_exact_visible_price,
    _parse_results,
)
from .app import NormalizedCriteria


OFFICIAL_ANA_SEARCH_ENTRY_URL = (
    "https://www.ana.co.jp/en/us/amc/international-flight-awards/"
)
APPROVED_ANA_DOMAINS = frozenset({"ana.co.jp", "www.ana.co.jp"})
REQUIRED_ANA_IDENTITY_DOMAINS = frozenset({"aswbe-i.ana.co.jp"})


class AnaPageKind(str, Enum):
    SEARCH_FORM = "search_form"
    RESULTS = "results"
    NO_AVAILABILITY = "no_availability"
    AUTHENTICATION = "authentication"
    SESSION_ERROR = "session_error"
    HEAVY_TRAFFIC = "heavy_traffic"
    VALIDATION_REJECTION = "validation_rejection"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AnaAgentOutcome:
    status: str
    page: PageSnapshot | None
    steps: int
    failures: int
    visited_urls: tuple[str, ...]
    actions: tuple[Mapping[str, Any], ...]
    detail: str = ""


class AnaBrowser(Protocol):
    def open(self, url: str) -> PageSnapshot: ...
    def deterministic_search(self, criteria: NormalizedCriteria) -> PageSnapshot: ...
    def browser_agent_search(
        self,
        criteria: NormalizedCriteria,
        *,
        goal: str,
        max_steps: int,
        max_actions_per_step: int,
        max_failures: int,
        allowed_domains: Sequence[str],
        authorize_action: Callable[[Mapping[str, Any]], None],
    ) -> AnaAgentOutcome: ...


class AnaSearchPolicy:
    """Constrain ANA automation to read-only award-search navigation and actions."""

    _SEARCH_PATHS = frozenset({
        "/en/us/amc/international-flight-awards",
        "/en/us/amc/international-flight-awards/",
    })
    _IDENTITY_PATH_PREFIXES = ("/international_asw/",)
    _ACTIONS = frozenset({
        "navigate", "fill_search_field", "select_search_option", "submit_search",
        "read_results",
    })
    _FIELDS = frozenset({
        "origin", "destination", "departure_date", "cabin", "adults", "trip_type",
    })
    _ACTION_KEYS = {
        "navigate": frozenset({"action", "url"}),
        "fill_search_field": frozenset({"action", "field", "value"}),
        "select_search_option": frozenset({"action", "field", "value"}),
        "submit_search": frozenset({"action"}),
        "read_results": frozenset({"action"}),
    }

    def __init__(self, allowed_domains: Sequence[str] | None = None) -> None:
        approved = APPROVED_ANA_DOMAINS | REQUIRED_ANA_IDENTITY_DOMAINS
        configured = approved if allowed_domains is None else frozenset(
            str(domain).strip().lower() for domain in allowed_domains
        )
        if not configured or not configured <= approved:
            raise PolicyViolation("allowed domains must be approved ANA domains")
        self.allowed_domains = frozenset(configured)
        self._confirmed_values: dict[str, str | int] | None = None
        self._entered_fields: set[str] = set()
        self._submission_seen = False

    @staticmethod
    def _normalized_field_value(field: str, value: str | int) -> str:
        normalized = str(value).strip()
        if field in {"origin", "destination"}:
            return normalized.upper()
        if field == "cabin":
            return " ".join(normalized.split()).casefold()
        if field == "trip_type":
            return normalized.casefold().replace("-", "_").replace(" ", "_")
        if field == "adults" and normalized.isdecimal():
            return str(int(normalized))
        return normalized

    def validate_url(self, url: str) -> None:
        if not isinstance(url, str):
            raise PolicyViolation("navigation URL must be a string")
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError as exc:
            raise PolicyViolation("navigation URL is malformed") from exc
        if (
            parsed.scheme != "https" or parsed.hostname not in self.allowed_domains
            or parsed.username is not None or parsed.password is not None
            or port not in {None, 443} or parsed.fragment
        ):
            raise PolicyViolation("navigation is outside approved ANA domains")
        decoded = parsed.path
        while unquote(decoded) != decoded:
            decoded = unquote(decoded)
        if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
            raise PolicyViolation("navigation path is malformed")
        if parsed.hostname in APPROVED_ANA_DOMAINS:
            if decoded not in self._SEARCH_PATHS or parsed.query:
                raise PolicyViolation("navigation is not an approved search-only ANA path")
        elif not any(decoded.startswith(prefix) for prefix in self._IDENTITY_PATH_PREFIXES):
            raise PolicyViolation("navigation is not a required ANA authentication path")

    def validate_action(self, action: Mapping[str, Any]) -> None:
        self._validate_action(action, record_submission=True)

    def validate_recorded_action(self, action: Mapping[str, Any]) -> None:
        """Revalidate an authorized trace without consuming submission state again."""
        self._validate_action(action, record_submission=False)

    def _validate_action(
        self, action: Mapping[str, Any], *, record_submission: bool,
    ) -> None:
        if not isinstance(action, Mapping):
            raise PolicyViolation("browser action must be structured data")
        if any(not isinstance(key, str) or key != key.strip().lower() for key in action):
            raise PolicyViolation("action keys must be canonical lowercase names")
        name = str(action.get("action", "")).strip().lower()
        if name not in self._ACTIONS:
            raise PolicyViolation("action is not permitted for ANA search-only automation")
        if set(action) - self._ACTION_KEYS[name]:
            raise PolicyViolation("action contains unsupported automation fields")
        if name == "navigate":
            self.validate_url(action.get("url", ""))
            if urlparse(str(action.get("url", ""))).hostname in REQUIRED_ANA_IDENTITY_DOMAINS:
                raise PolicyViolation("agent navigation to identity domains is not permitted")
        elif name in {"fill_search_field", "select_search_option"}:
            field = str(action.get("field", "")).strip().lower().replace("-", "_")
            if field not in self._FIELDS or "value" not in action:
                raise PolicyViolation("action does not target an approved ANA search field")
            if not isinstance(action["value"], (str, int)) or isinstance(action["value"], bool):
                raise PolicyViolation("search field value must be scalar structured data")
            if self._confirmed_values is None:
                raise PolicyViolation("ANA search criteria must be confirmed before interaction")
            if self._normalized_field_value(field, action["value"]) != self._normalized_field_value(
                field, self._confirmed_values[field]
            ):
                raise PolicyViolation("search field value differs from confirmed criteria")
            self._entered_fields.add(field)
        elif name == "submit_search" and record_submission:
            if self._entered_fields != self._FIELDS:
                raise PolicyViolation("all confirmed criteria must be entered before ANA search submission")
            if self._submission_seen:
                raise PolicyViolation("ANA search may be submitted at most once")
            self._submission_seen = True

    def validate_criteria(self, criteria: NormalizedCriteria) -> None:
        if criteria.program != "ana":
            raise PolicyViolation("ANA adapter supports ANA Mileage Club only")
        if criteria.trip_type != "one_way" or criteria.adults != 1:
            raise PolicyViolation("controlled ANA search supports one-way travel for one adult")
        if criteria.cabin not in {"Economy", "Premium Economy", "Business", "First"}:
            raise PolicyViolation("unsupported ANA cabin")
        self._confirmed_values = {
            "origin": criteria.origin,
            "destination": criteria.destination,
            "departure_date": criteria.departure_date,
            "cabin": criteria.cabin,
            "adults": criteria.adults,
            "trip_type": criteria.trip_type,
        }
        self._entered_fields = set()
        self._submission_seen = False


def classify_ana_page(page: PageSnapshot) -> AnaPageKind:
    parser = _ResultsScriptParser()
    parser.feed(page.html)
    explicit = {
        "search_form": AnaPageKind.SEARCH_FORM,
        "results": AnaPageKind.RESULTS,
        "no_availability": AnaPageKind.NO_AVAILABILITY,
        "authentication": AnaPageKind.AUTHENTICATION,
        "session_error": AnaPageKind.SESSION_ERROR,
        "heavy_traffic": AnaPageKind.HEAVY_TRAFFIC,
        "validation_rejection": AnaPageKind.VALIDATION_REJECTION,
    }
    if parser.page_kind in explicit:
        return explicit[parser.page_kind]
    text = " ".join(parser.visible_text).lower()
    if "log in" in text or "login required" in text:
        return AnaPageKind.AUTHENTICATION
    if "session has expired" in text or "session error" in text:
        return AnaPageKind.SESSION_ERROR
    if "heavy traffic" in text or "temporarily congested" in text or "access denied" in text:
        return AnaPageKind.HEAVY_TRAFFIC
    return AnaPageKind.UNKNOWN


def _validated_ana_itinerary(
    raw: Mapping[str, Any], criteria: NormalizedCriteria,
    visible_text: list[tuple[str, str]],
) -> tuple[dict[str, Any] | None, str | None]:
    import re

    points = raw.get("points_per_passenger")
    label = raw.get("price_label")
    if (
        raw.get("visible") is not True or raw.get("complete_itinerary") is not True
        or raw.get("price_kind") != "exact" or type(points) is not int
        or not isinstance(label, str)
        or re.fullmatch(r"\s*\d[\d,]*\s+(?:mile|miles)\s*", label, re.I) is None
        or int(re.sub(r"\D", "", label)) != points
    ):
        return None, "UNVERIFIED_PRICE"
    cash = raw.get("cash")
    segments = raw.get("segments")
    required = (
        "origin", "destination", "departure", "arrival", "flight_number",
        "marketing_carrier", "operating_carrier", "cabin",
    )
    if (
        not isinstance(cash, Mapping)
        or any(not isinstance(cash.get(field), str) or not cash.get(field) for field in ("displayed_total", "currency"))
        or not isinstance(segments, list) or not segments
        or any(not isinstance(segment, Mapping) or any(not isinstance(segment.get(field), str) or not segment.get(field) for field in required) for segment in segments)
    ):
        return None, "PARSER_FAILED"
    try:
        departure_date = datetime.fromisoformat(segments[0]["departure"]).date().isoformat()
    except ValueError:
        return None, "PARSER_FAILED"
    if (
        segments[0]["origin"].upper() != criteria.origin
        or segments[-1]["destination"].upper() != criteria.destination
        or departure_date != criteria.departure_date
    ):
        return None, "PARSER_FAILED"
    if not _is_exact_visible_price(
        label, cash["displayed_total"], cash["currency"], visible_text,
        [segment["flight_number"] for segment in segments],
    ):
        return None, "UNVERIFIED_PRICE"
    cabins = {segment["cabin"] for segment in segments}
    warnings = raw.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(item, str) or not item.strip() for item in warnings):
        return None, "PARSER_FAILED"
    mixed = len(cabins) != 1 or cabins != {criteria.cabin}
    return {
        "points_per_passenger": points,
        "points_total": points * criteria.adults,
        "cash": {"displayed_total": cash["displayed_total"], "currency": cash["currency"]},
        "segments": [dict(segment) for segment in segments],
        "connections": max(len(segments) - 1, 0),
        "warnings": sorted(set(warnings + (["mixed_cabin"] if mixed else []))),
        "mixed_cabin": mixed,
    }, None


class AnaSearchAdapter:
    def __init__(
        self, *, browser: AnaBrowser, policy: AnaSearchPolicy | None = None,
        max_agent_steps: int = 8, max_actions_per_step: int = 2, max_failures: int = 2,
    ) -> None:
        if min(max_agent_steps, max_actions_per_step, max_failures) < 1:
            raise ValueError("ANA browser bounds must be positive")
        self.browser = browser
        self.policy = policy or AnaSearchPolicy()
        self.max_agent_steps = max_agent_steps
        self.max_actions_per_step = max_actions_per_step
        self.max_failures = max_failures

    def execute(self, criteria: NormalizedCriteria, atomic_task_id: str) -> dict[str, Any]:
        del atomic_task_id
        try:
            self.policy.validate_criteria(criteria)
            self.policy.validate_url(OFFICIAL_ANA_SEARCH_ENTRY_URL)
            page = self.browser.open(OFFICIAL_ANA_SEARCH_ENTRY_URL)
            self.policy.validate_url(page.url)
        except PolicyViolation as exc:
            return self._result("POLICY_BLOCKED", str(exc))
        except (SearchTimeout, TimeoutError):
            return self._result("SEARCH_TIMEOUT", "ANA search timed out")

        kind = classify_ana_page(page)
        used_agent = False
        if kind not in {
            AnaPageKind.AUTHENTICATION, AnaPageKind.SESSION_ERROR,
            AnaPageKind.HEAVY_TRAFFIC, AnaPageKind.VALIDATION_REJECTION,
        }:
            try:
                page = self.browser.deterministic_search(criteria)
                self.policy.validate_url(page.url)
                kind = classify_ana_page(page)
            except UnsupportedDeterministicLayout:
                kind = AnaPageKind.UNKNOWN
            except PolicyViolation as exc:
                return self._result("POLICY_BLOCKED", str(exc))
            except (SearchTimeout, TimeoutError):
                return self._result("SEARCH_TIMEOUT", "ANA search timed out")

        if kind == AnaPageKind.UNKNOWN:
            try:
                outcome = self.browser.browser_agent_search(
                    criteria,
                    goal=("Reach the official ANA Mileage Club international award result page "
                          "using only the already-confirmed one-way search criteria; do not retry."),
                    max_steps=self.max_agent_steps,
                    max_actions_per_step=self.max_actions_per_step,
                    max_failures=self.max_failures,
                    allowed_domains=sorted(self.policy.allowed_domains),
                    authorize_action=self.policy.validate_action,
                )
                self._validate_agent_outcome(outcome)
            except (PolicyViolation, ValueError) as exc:
                return self._result("VALIDATION_REJECTED", str(exc))
            except (SearchTimeout, TimeoutError):
                return self._result("SEARCH_TIMEOUT", "ANA browser agent timed out")
            exhausted = {
                "step_exhausted": "AGENT_STEP_EXHAUSTED",
                "action_exhausted": "AGENT_ACTION_EXHAUSTED",
                "failure_exhausted": "AGENT_FAILURE_EXHAUSTED",
            }
            if outcome.status in exhausted:
                return self._result(exhausted[outcome.status], outcome.detail or outcome.status)
            if outcome.status != "completed" or outcome.page is None:
                return self._result("BROWSER_AGENT_FAILED", outcome.detail or "ANA browser agent failed")
            page = outcome.page
            kind = classify_ana_page(page)
            used_agent = True

        terminal = {
            AnaPageKind.AUTHENTICATION: ("AUTHENTICATION_REQUIRED", "ANA Mileage Club login is required"),
            AnaPageKind.SESSION_ERROR: ("SESSION_ERROR", "ANA reported an expired or invalid session"),
            AnaPageKind.HEAVY_TRAFFIC: ("HEAVY_TRAFFIC_BLOCKED", "ANA reported heavy traffic or blocking"),
            AnaPageKind.NO_AVAILABILITY: ("NO_AWARD_AVAILABILITY", "No ANA Mileage Club award availability"),
            AnaPageKind.VALIDATION_REJECTION: ("VALIDATION_REJECTED", "ANA result page failed deterministic validation"),
            AnaPageKind.UNKNOWN: ("PARSER_FAILED", "unknown ANA page after bounded search"),
            AnaPageKind.SEARCH_FORM: ("PARSER_FAILED", "ANA search did not reach results"),
        }
        if kind in terminal:
            status, detail = terminal[kind]
            return self._result(status, detail, browser_agent_used=used_agent)

        try:
            raw_itineraries, visible_text = _parse_results(page)
        except ValueError as exc:
            return self._result("VALIDATION_REJECTED", str(exc), browser_agent_used=used_agent)
        valid: list[dict[str, Any]] = []
        saw_unverified = False
        saw_above = False
        for raw in raw_itineraries:
            if not isinstance(raw, Mapping):
                return self._result("VALIDATION_REJECTED", "itinerary is not structured data")
            itinerary, failure = _validated_ana_itinerary(raw, criteria, visible_text)
            if failure == "PARSER_FAILED":
                return self._result("VALIDATION_REJECTED", "itinerary failed schema validation")
            if failure == "UNVERIFIED_PRICE":
                saw_unverified = True
                continue
            assert itinerary is not None
            if itinerary["mixed_cabin"]:
                return self._result("VALIDATION_REJECTED", "mixed-cabin ANA itinerary rejected")
            if itinerary["points_per_passenger"] > criteria.maximum_points:
                saw_above = True
                continue
            valid.append(itinerary)
        if valid:
            return self._result(
                "MATCH_FOUND", "Found a complete visible ANA itinerary within the miles limit",
                itineraries=valid, browser_agent_used=used_agent,
            )
        if saw_unverified:
            return self._result("UNVERIFIED_PRICE", "ANA price was not an exact visible itinerary price", browser_agent_used=used_agent)
        if saw_above:
            return self._result("ABOVE_POINTS_LIMIT", "ANA itinerary exceeds the per-passenger miles limit", browser_agent_used=used_agent)
        return self._result("VALIDATION_REJECTED", "ANA extraction was empty without explicit no availability", browser_agent_used=used_agent)

    def _validate_agent_outcome(self, outcome: AnaAgentOutcome) -> None:
        if not isinstance(outcome, AnaAgentOutcome):
            raise ValueError("ANA browser agent must return structured output")
        allowed_statuses = {"completed", "failed", "refused", "step_exhausted", "action_exhausted", "failure_exhausted"}
        if outcome.status not in allowed_statuses:
            raise ValueError("ANA browser agent returned an invalid status")
        if type(outcome.steps) is not int or not 0 <= outcome.steps <= self.max_agent_steps:
            raise PolicyViolation("ANA browser agent exceeded its step bound")
        if type(outcome.failures) is not int or not 0 <= outcome.failures <= self.max_failures:
            raise PolicyViolation("ANA browser agent exceeded its failure bound")
        if not isinstance(outcome.actions, tuple) or not isinstance(outcome.visited_urls, tuple):
            raise ValueError("ANA browser agent trace must be immutable structured data")
        if len(outcome.actions) > outcome.steps * self.max_actions_per_step:
            raise PolicyViolation("ANA browser agent exceeded its action bound")
        for action in outcome.actions:
            self.policy.validate_recorded_action(action)
        for url in outcome.visited_urls:
            self.policy.validate_url(url)
        if outcome.page is not None:
            self.policy.validate_url(outcome.page.url)
            if outcome.page.url not in outcome.visited_urls:
                raise ValueError("ANA final page is absent from the navigation trace")

    @staticmethod
    def _result(status: str, detail: str, **fields: Any) -> dict[str, Any]:
        return {"status": status, "detail": detail, "search_entry_url": OFFICIAL_ANA_SEARCH_ENTRY_URL, **fields}


class AnaFixtureBrowser:
    def __init__(
        self, result_fixture: Path, *, initial_fixture: Path | None = None,
        deterministic_supported: bool = True, agent_outcome: AnaAgentOutcome | None = None,
        timeout_at: str | None = None,
    ) -> None:
        self.result_fixture = Path(result_fixture)
        self.initial_fixture = Path(initial_fixture) if initial_fixture else None
        self.deterministic_supported = deterministic_supported
        self.agent_outcome = agent_outcome
        self.timeout_at = timeout_at
        self.calls: list[str] = []

    def _page(self, path: Path) -> PageSnapshot:
        return PageSnapshot(OFFICIAL_ANA_SEARCH_ENTRY_URL, path.read_text(encoding="utf-8"))

    def open(self, url: str) -> PageSnapshot:
        del url
        self.calls.append("open")
        if self.timeout_at == "open":
            raise SearchTimeout
        return self._page(self.initial_fixture or self.result_fixture)

    def deterministic_search(self, criteria: NormalizedCriteria) -> PageSnapshot:
        del criteria
        self.calls.append("deterministic_search")
        if self.timeout_at == "deterministic_search":
            raise SearchTimeout
        if not self.deterministic_supported:
            raise UnsupportedDeterministicLayout
        return self._page(self.result_fixture)

    def browser_agent_search(self, criteria: NormalizedCriteria, **kwargs: Any) -> AnaAgentOutcome:
        del criteria
        self.calls.append("browser_agent_search")
        if self.timeout_at == "browser_agent_search":
            raise SearchTimeout
        if self.agent_outcome is not None:
            for action in self.agent_outcome.actions:
                kwargs["authorize_action"](action)
            return self.agent_outcome
        page = self._page(self.result_fixture)
        action = {"action": "read_results"}
        kwargs["authorize_action"](action)
        return AnaAgentOutcome(
            status="completed", page=page, steps=1, failures=0,
            visited_urls=(page.url,), actions=(action,),
        )
