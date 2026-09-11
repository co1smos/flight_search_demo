from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import unquote, urlparse

from .app import NormalizedCriteria


OFFICIAL_SEARCH_ENTRY_URL = "https://www.aircanada.com/aeroplan/redeem/availability"
APPROVED_AIR_CANADA_DOMAINS = frozenset({
    "aircanada.com",
    "www.aircanada.com",
    "aeroplan.com",
    "www.aeroplan.com",
})
REQUIRED_IDENTITY_DOMAINS = frozenset({
    "login.aircanada.com",
    "aircanada.b2clogin.com",
})


class PageKind(str, Enum):
    SEARCH_FORM = "search_form"
    RESULTS = "results"
    NO_AVAILABILITY = "no_availability"
    AUTHENTICATION = "authentication"
    CHALLENGE = "challenge"
    SITE_ERROR = "site_error"
    UNKNOWN = "unknown"


class PolicyViolation(ValueError):
    pass


class UnsupportedDeterministicLayout(RuntimeError):
    pass


class SearchTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    html: str


@dataclass(frozen=True)
class BrowserAgentOutcome:
    status: str
    page: PageSnapshot | None
    steps: int
    visited_urls: tuple[str, ...]
    actions: tuple[Mapping[str, Any], ...]
    detail: str = ""


class AeroplanBrowser(Protocol):
    def open(self, url: str) -> PageSnapshot: ...

    def deterministic_search(self, criteria: NormalizedCriteria) -> PageSnapshot: ...

    def browser_agent_search(
        self,
        criteria: NormalizedCriteria,
        *,
        max_steps: int,
        allowed_domains: Sequence[str],
        authorize_action: Callable[[Mapping[str, Any]], None],
    ) -> BrowserAgentOutcome: ...


class SearchPolicy:
    _ALLOWED_ACTIONS = frozenset({
        "navigate",
        "fill_search_field",
        "select_search_option",
        "submit_search",
        "read_results",
    })
    _AIR_CANADA_SEARCH_PATHS = (
        "/aeroplan/redeem/availability",
        "/search",
    )
    _ALLOWED_SEARCH_FIELDS = frozenset({
        "adults",
        "cabin",
        "date",
        "departure_date",
        "destination",
        "origin",
        "passenger_count",
        "passengers",
        "trip_type",
    })
    _ALLOWED_ACTION_KEYS = {
        "navigate": frozenset({"action", "url"}),
        "fill_search_field": frozenset({"action", "field", "value"}),
        "select_search_option": frozenset({"action", "field", "value"}),
        "submit_search": frozenset({"action"}),
        "read_results": frozenset({"action"}),
    }

    def __init__(self, *, allowed_domains: Sequence[str] | None = None) -> None:
        approved_domains = APPROVED_AIR_CANADA_DOMAINS | REQUIRED_IDENTITY_DOMAINS
        if allowed_domains is None:
            self.allowed_domains = approved_domains
            return
        if any(
            not isinstance(domain, str)
            or not domain.strip()
            or domain.strip().lower() not in approved_domains
            for domain in allowed_domains
        ):
            raise PolicyViolation("allowed domains must be approved Air Canada domains")
        configured_domains = frozenset(domain.strip().lower() for domain in allowed_domains)
        self.allowed_domains = configured_domains

    def validate_url(self, url: str) -> None:
        if not isinstance(url, str):
            raise PolicyViolation("navigation URL must be a string")
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError as exc:
            raise PolicyViolation("navigation URL is malformed") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.allowed_domains
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
        ):
            raise PolicyViolation("navigation is outside approved Air Canada domains")
        decoded_path = parsed.path
        while True:
            next_path = unquote(decoded_path)
            if next_path == decoded_path:
                break
            decoded_path = next_path
        if "\\" in decoded_path:
            raise PolicyViolation("navigation path must not contain backslashes")
        if any(segment in {".", ".."} for segment in decoded_path.split("/")):
            raise PolicyViolation("navigation path must not contain dot segments")
        if parsed.hostname in APPROVED_AIR_CANADA_DOMAINS and not any(
            parsed.path == prefix or parsed.path.startswith(f"{prefix}/")
            for prefix in self._AIR_CANADA_SEARCH_PATHS
        ):
            raise PolicyViolation("navigation is not an approved search-only Air Canada path")

    def validate_action(self, action: Mapping[str, Any]) -> None:
        keys = list(action)
        if any(
            not isinstance(key, str) or key != key.strip().lower()
            for key in keys
        ):
            raise PolicyViolation("action keys must use canonical lowercase names")
        normalized_keys = [key.strip().lower() for key in keys]
        if len(normalized_keys) != len(set(normalized_keys)):
            raise PolicyViolation("action contains duplicate normalized fields")
        name = str(action.get("action", "")).strip().lower()
        if name not in self._ALLOWED_ACTIONS:
            raise PolicyViolation(f"action is not permitted for search-only automation: {name}")
        if set(normalized_keys) - self._ALLOWED_ACTION_KEYS[name]:
            raise PolicyViolation("action contains unsupported automation fields")
        if name == "navigate":
            url = str(action.get("url", ""))
            self.validate_url(url)
            if urlparse(url).hostname in REQUIRED_IDENTITY_DOMAINS:
                raise PolicyViolation("agent navigation to identity domains is not permitted")
            return
        if name in {"fill_search_field", "select_search_option"}:
            field = str(action.get("field", "")).strip().lower().replace("-", "_")
            if field not in self._ALLOWED_SEARCH_FIELDS or "value" not in action:
                raise PolicyViolation("action does not target an approved award-search field")
            if not isinstance(action["value"], (str, int)) or isinstance(action["value"], bool):
                raise PolicyViolation("search field value must be scalar structured data")


class _ResultsScriptParser(HTMLParser):
    _VOID_ELEMENTS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )
    _NON_VISIBLE_ELEMENTS = frozenset({"head", "script", "style", "template"})
    _TEXT_REGION_ELEMENTS = frozenset(
        {
            "article",
            "aside",
            "body",
            "button",
            "dd",
            "div",
            "dl",
            "dt",
            "fieldset",
            "figcaption",
            "figure",
            "footer",
            "form",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "section",
            "table",
            "tbody",
            "td",
            "tfoot",
            "th",
            "thead",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self.page_kind: str | None = None
        self._in_results_script = False
        self._element_stack: list[tuple[str, bool, list[str]]] = []
        self.results_json = ""
        self.visible_text: list[str] = []
        self.visible_regions: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self.page_kind is None and attributes.get("data-page-kind"):
            self.page_kind = attributes["data-page-kind"]
        if tag == "script" and attributes.get("id") == "aeroplan-results-data":
            self._in_results_script = True
        parent_hidden = self._element_stack[-1][1] if self._element_stack else False
        style = attributes.get("style", "") or ""
        style_declarations = {
            name.strip().lower(): value.strip().lower().removesuffix("!important").strip()
            for declaration in style.split(";")
            if ":" in declaration
            for name, value in [declaration.split(":", 1)]
        }
        element_hidden = (
            parent_hidden
            or tag in self._NON_VISIBLE_ELEMENTS
            or "hidden" in attributes
            or (attributes.get("aria-hidden") or "").strip().lower() == "true"
            or style_declarations.get("display") == "none"
            or style_declarations.get("visibility") in {"hidden", "collapse"}
            or style_declarations.get("content-visibility") == "hidden"
        )
        if tag not in self._VOID_ELEMENTS:
            self._element_stack.append((tag, element_hidden, []))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_results_script:
            self._in_results_script = False
        for index in range(len(self._element_stack) - 1, -1, -1):
            if self._element_stack[index][0] == tag:
                _, hidden, text_parts = self._element_stack[index]
                if tag in self._TEXT_REGION_ELEMENTS and not hidden and text_parts:
                    self.visible_regions.append(" ".join(text_parts))
                del self._element_stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._in_results_script:
            self.results_json += data
        elif data.strip() and not (
            self._element_stack and self._element_stack[-1][1]
        ):
            text = data.strip()
            self.visible_text.append(text)
            for _, hidden, text_parts in self._element_stack:
                if not hidden:
                    text_parts.append(text)


def classify_page(page: PageSnapshot) -> PageKind:
    parser = _ResultsScriptParser()
    parser.feed(page.html)
    visible = " ".join(parser.visible_text).lower()
    hostname = (urlparse(page.url).hostname or "").lower()
    if hostname in REQUIRED_IDENTITY_DOMAINS or re.search(
        r"\b(?:sign in to aeroplan|log in to aeroplan)\b", visible
    ):
        return PageKind.AUTHENTICATION
    if re.search(r"\b(?:verify you are human|access denied|captcha|bot detection)\b", visible):
        return PageKind.CHALLENGE
    if re.search(
        r"\b(?:temporarily unable|technical difficulties|something went wrong)\b", visible
    ):
        return PageKind.SITE_ERROR
    try:
        return PageKind(parser.page_kind or "unknown")
    except ValueError:
        return PageKind.UNKNOWN


def _parse_results(page: PageSnapshot) -> tuple[list[dict[str, Any]], list[str]]:
    parser = _ResultsScriptParser()
    parser.feed(page.html)
    if not parser.results_json.strip():
        raise ValueError("results page has no deterministic extraction payload")
    try:
        payload = json.loads(parser.results_json)
    except json.JSONDecodeError as exc:
        raise ValueError("results extraction payload is malformed") from exc
    itineraries = payload.get("itineraries")
    if not isinstance(itineraries, list):
        raise ValueError("results extraction payload has no itinerary list")
    return itineraries, parser.visible_regions


def _is_exact_visible_price(
    value: str, displayed_total: str, currency: str, visible_text: list[str]
) -> bool:
    expected = " ".join(f"{value} + {displayed_total} {currency}".split()).casefold()
    return any(" ".join(text.split()).casefold() == expected for text in visible_text)


def _validated_itinerary(
    raw: Mapping[str, Any], criteria: NormalizedCriteria, visible_text: list[str]
) -> tuple[dict[str, Any] | None, str | None]:
    points = raw.get("points_per_passenger")
    label = raw.get("price_label")
    if (
        "visible" not in raw
        or "complete_itinerary" not in raw
        or "price_kind" not in raw
        or "price_label" not in raw
        or "points_per_passenger" not in raw
        or type(points) is not int
        or not isinstance(label, str)
    ):
        return None, "PARSER_FAILED"
    if (
        raw.get("visible") is not True
        or raw.get("complete_itinerary") is not True
        or raw.get("price_kind") != "exact"
        or re.fullmatch(r"\s*\d[\d,]*\s+(?:pts|points)\s*", label, re.IGNORECASE) is None
        or int(re.sub(r"\D", "", label)) != points
    ):
        return None, "UNVERIFIED_PRICE"

    cash = raw.get("cash")
    if not isinstance(cash, Mapping) or not all(
        isinstance(cash.get(field), str) and cash.get(field)
        for field in ("displayed_total", "currency")
    ):
        return None, "PARSER_FAILED"
    if not _is_exact_visible_price(
        label, cash["displayed_total"], cash["currency"], visible_text
    ):
        return None, "UNVERIFIED_PRICE"
    segments = raw.get("segments")
    required_segment_fields = (
        "departure",
        "arrival",
        "flight_number",
        "marketing_carrier",
        "operating_carrier",
        "cabin",
    )
    if not isinstance(segments, list) or not segments or any(
        not isinstance(segment, Mapping)
        or any(not isinstance(segment.get(field), str) or not segment.get(field)
               for field in required_segment_fields)
        for segment in segments
    ):
        return None, "PARSER_FAILED"
    cabins = {segment["cabin"] for segment in segments}
    warnings_payload = raw.get("warnings", [])
    if not isinstance(warnings_payload, list) or any(
        not isinstance(item, str) or not item.strip() for item in warnings_payload
    ):
        return None, "PARSER_FAILED"
    warnings = list(warnings_payload)
    mixed_cabin = len(cabins) != 1 or cabins != {criteria.cabin}
    if mixed_cabin:
        warnings.append("mixed_cabin")

    itinerary = {
        "points_per_passenger": points,
        "points_total": points * criteria.adults,
        "cash": {
            "displayed_total": cash["displayed_total"],
            "currency": cash["currency"],
        },
        "segments": [dict(segment) for segment in segments],
        "connections": max(len(segments) - 1, 0),
        "warnings": sorted(set(str(item) for item in warnings)),
        "mixed_cabin": mixed_cabin,
    }
    return itinerary, None


class AeroplanSearchAdapter:
    def __init__(
        self,
        *,
        browser: AeroplanBrowser,
        policy: SearchPolicy | None = None,
        max_agent_steps: int = 6,
    ) -> None:
        if max_agent_steps < 1:
            raise ValueError("max_agent_steps must be positive")
        self.browser = browser
        self.policy = policy or SearchPolicy()
        self.max_agent_steps = max_agent_steps

    def execute(self, criteria: NormalizedCriteria, atomic_task_id: str) -> dict[str, Any]:
        del atomic_task_id
        self.policy.validate_url(OFFICIAL_SEARCH_ENTRY_URL)
        try:
            page = self.browser.open(OFFICIAL_SEARCH_ENTRY_URL)
            self.policy.validate_url(page.url)
        except PolicyViolation as exc:
            return self._result("POLICY_BLOCKED", str(exc))
        except (SearchTimeout, TimeoutError):
            return self._result("SEARCH_TIMEOUT", "Aeroplan search timed out")

        kind = classify_page(page)
        used_agent = False
        if kind not in {PageKind.AUTHENTICATION, PageKind.CHALLENGE, PageKind.SITE_ERROR}:
            try:
                page = self.browser.deterministic_search(criteria)
                self.policy.validate_url(page.url)
                kind = classify_page(page)
            except UnsupportedDeterministicLayout:
                kind = PageKind.UNKNOWN
            except PolicyViolation as exc:
                return self._result("POLICY_BLOCKED", str(exc))
            except (SearchTimeout, TimeoutError):
                return self._result("SEARCH_TIMEOUT", "Aeroplan search timed out")

        if kind == PageKind.UNKNOWN:
            try:
                outcome = self.browser.browser_agent_search(
                    criteria,
                    max_steps=self.max_agent_steps,
                    allowed_domains=sorted(self.policy.allowed_domains),
                    authorize_action=self.policy.validate_action,
                )
                self._validate_agent_outcome(outcome)
            except (PolicyViolation, ValueError) as exc:
                return self._result("BROWSER_AGENT_FAILED", str(exc))
            except (SearchTimeout, TimeoutError):
                return self._result("SEARCH_TIMEOUT", "Aeroplan browser fallback timed out")
            if outcome.status != "completed" or outcome.page is None:
                return self._result("BROWSER_AGENT_FAILED", outcome.detail or "browser fallback failed")
            page = outcome.page
            kind = classify_page(page)
            used_agent = True

        terminal = {
            PageKind.AUTHENTICATION: ("AUTHENTICATION_REQUIRED", "Aeroplan authentication is required"),
            PageKind.CHALLENGE: ("CHALLENGE_BLOCKED", "Air Canada challenge or blocking page"),
            PageKind.SITE_ERROR: ("SITE_ERROR", "Air Canada reported a site error"),
            PageKind.NO_AVAILABILITY: ("NO_AWARD_AVAILABILITY", "No Aeroplan award availability"),
            PageKind.UNKNOWN: ("PARSER_FAILED", "unknown Aeroplan page after bounded search"),
            PageKind.SEARCH_FORM: ("PARSER_FAILED", "search did not reach a result page"),
        }
        if kind in terminal:
            status, detail = terminal[kind]
            return self._result(status, detail, browser_agent_used=used_agent)

        try:
            raw_itineraries, visible_text = _parse_results(page)
        except ValueError as exc:
            return self._result("PARSER_FAILED", str(exc), browser_agent_used=used_agent)

        valid: list[dict[str, Any]] = []
        saw_unverified = False
        saw_above_limit = False
        lowest_above_limit: int | None = None
        saw_mixed_cabin = False
        rejected_itineraries: list[dict[str, Any]] = []
        for raw in raw_itineraries:
            if not isinstance(raw, Mapping):
                return self._result("PARSER_FAILED", "itinerary is not structured data")
            itinerary, failure = _validated_itinerary(raw, criteria, visible_text)
            if failure == "PARSER_FAILED":
                return self._result("PARSER_FAILED", "itinerary is missing required fields")
            if failure == "UNVERIFIED_PRICE":
                saw_unverified = True
                continue
            assert itinerary is not None
            if itinerary["mixed_cabin"]:
                saw_mixed_cabin = True
                rejected_itineraries.append(itinerary)
                continue
            if itinerary["points_per_passenger"] > criteria.maximum_points:
                saw_above_limit = True
                rejected_itineraries.append(itinerary)
                if lowest_above_limit is None:
                    lowest_above_limit = itinerary["points_per_passenger"]
                else:
                    lowest_above_limit = min(
                        lowest_above_limit, itinerary["points_per_passenger"]
                    )
                continue
            valid.append(itinerary)

        if valid:
            return self._result(
                "MATCH_FOUND",
                "Found a complete visible Aeroplan itinerary within the per-passenger points limit",
                itineraries=valid,
                browser_agent_used=used_agent,
            )
        if saw_mixed_cabin:
            return self._result(
                "NO_QUALIFYING_ITINERARY",
                "Only mixed-cabin itineraries were found",
                itineraries=rejected_itineraries,
                quality_warnings=["mixed_cabin"],
                browser_agent_used=used_agent,
            )
        if saw_unverified:
            return self._result(
                "UNVERIFIED_PRICE",
                "Only from, inferred, hidden, or incomplete-itinerary prices were found",
                browser_agent_used=used_agent,
            )
        if saw_above_limit:
            return self._result(
                "ABOVE_POINTS_LIMIT",
                f"Visible complete-itinerary price of {lowest_above_limit} points per passenger "
                "exceeds the points limit",
                itineraries=rejected_itineraries,
                browser_agent_used=used_agent,
            )
        return self._result(
            "NO_AWARD_AVAILABILITY",
            "No qualifying Aeroplan award itinerary was extracted",
            browser_agent_used=used_agent,
        )

    def _validate_agent_outcome(self, outcome: BrowserAgentOutcome) -> None:
        if not isinstance(outcome, BrowserAgentOutcome):
            raise ValueError("browser fallback must return a structured outcome")
        if outcome.status not in {"completed", "failed", "refused"}:
            raise ValueError("browser fallback returned an invalid status")
        if (
            type(outcome.steps) is not int
            or outcome.steps < 0
            or outcome.steps > self.max_agent_steps
        ):
            raise PolicyViolation("browser fallback exceeded its step bound")
        if not isinstance(outcome.visited_urls, tuple) or not isinstance(outcome.actions, tuple):
            raise ValueError("browser fallback trace must be immutable structured data")
        if len(outcome.actions) > outcome.steps:
            raise PolicyViolation("browser fallback action trace exceeds its reported step count")
        for url in outcome.visited_urls:
            self.policy.validate_url(url)
        for action in outcome.actions:
            if not isinstance(action, Mapping):
                raise ValueError("browser fallback action must be structured data")
            self.policy.validate_action(action)
        if outcome.page is not None and not isinstance(outcome.page, PageSnapshot):
            raise ValueError("browser fallback page must be a structured snapshot")
        if outcome.page is not None:
            self.policy.validate_url(outcome.page.url)
            if outcome.page.url not in outcome.visited_urls:
                raise ValueError("browser fallback final page is missing from its navigation trace")

    @staticmethod
    def _result(status: str, detail: str, **fields: Any) -> dict[str, Any]:
        return {
            "status": status,
            "detail": detail,
            "search_entry_url": OFFICIAL_SEARCH_ENTRY_URL,
            **fields,
        }


class AeroplanFixtureBrowser:
    """Offline browser double used by the fixture-backed slice and its tests."""

    def __init__(
        self,
        result_fixture: Path,
        *,
        initial_fixture: Path | None = None,
        deterministic_supported: bool = True,
        agent_outcome: BrowserAgentOutcome | None = None,
        timeout_at: str | None = None,
    ) -> None:
        self.result_fixture = Path(result_fixture)
        self.initial_fixture = Path(initial_fixture) if initial_fixture else None
        self.deterministic_supported = deterministic_supported
        self.agent_outcome = agent_outcome
        self.timeout_at = timeout_at
        self.calls: list[str] = []

    def _page(self, path: Path) -> PageSnapshot:
        return PageSnapshot(OFFICIAL_SEARCH_ENTRY_URL, path.read_text(encoding="utf-8"))

    def open(self, url: str) -> PageSnapshot:
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

    def browser_agent_search(
        self,
        criteria: NormalizedCriteria,
        *,
        max_steps: int,
        allowed_domains: Sequence[str],
        authorize_action: Callable[[Mapping[str, Any]], None],
    ) -> BrowserAgentOutcome:
        del criteria, max_steps, allowed_domains
        self.calls.append("browser_agent_search")
        if self.timeout_at == "browser_agent_search":
            raise SearchTimeout
        if self.agent_outcome is not None:
            for action in self.agent_outcome.actions:
                authorize_action(action)
            return self.agent_outcome
        page = self._page(self.result_fixture)
        action = {"action": "read_results"}
        authorize_action(action)
        return BrowserAgentOutcome(
            status="completed",
            page=page,
            steps=1,
            visited_urls=(page.url,),
            actions=(action,),
        )
