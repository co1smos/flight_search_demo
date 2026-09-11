from __future__ import annotations

import argparse
import fcntl
import hashlib
import inspect
import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .gemini_policy import (
    DEFAULT_DAILY_CALL_LIMITS,
    GeminiCallPolicy,
    GeminiErrorClassification,
    GeminiCallFailure,
    GeminiPolicyConfig,
    GeminiPolicyError,
    MalformedModelOutputError,
    redact_sensitive,
    redact_sensitive_text,
    validate_model_configuration,
)

SUPPORTED_CABINS = {
    "economy": "Economy",
    "premium_economy": "Premium Economy",
    "business": "Business",
    "first": "First",
}
PROGRAM_ALIASES = {
    "aeroplan": "aeroplan", "air canada aeroplan": "aeroplan", "ac points": "aeroplan",
    "ana": "ana", "ana mileage club": "ana", "ana miles": "ana",
}
PROGRAM_SELECTION_STATES = {"omitted", "supported", "unsupported", "ambiguous"}
SHARED_PROGRAM_CRITERIA_FIELDS = (
    "origin",
    "destination",
    "departure_date",
    "cabin",
    "adults",
    "trip_type",
    "maximum_points",
)
PARSER_REQUIRED_FIELDS = (
    "origin",
    "destination",
    "departure_date",
    "cabin",
    "adults",
    "maximum_points",
)
ROUTE_ENDPOINT_TOKEN = r"[A-Za-z][A-Za-z'’]*(?:-[A-Za-z][A-Za-z'’]*)?"
ROUTE_ENDPOINT_EXPRESSION = (
    rf"{ROUTE_ENDPOINT_TOKEN}(?:\s+{ROUTE_ENDPOINT_TOKEN}){{0,3}}"
)
ROUTE_ENDPOINT_EXPRESSION_LAZY = (
    rf"{ROUTE_ENDPOINT_TOKEN}(?:\s+{ROUTE_ENDPOINT_TOKEN}){{0,3}}?"
)
ROUTE_BOUNDARY = (
    r"(?=\s+(?:on|for|in|under|below|at\s+most|up\s+to|maximum|max|"
    r"ceiling|next|this|coming|today|tomorrow|yesterday|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\b|[,.!?]|$))"
)
ROUTE_PATTERNS = (
    re.compile(
        rf"\bfrom\s+(?P<origin>{ROUTE_ENDPOINT_EXPRESSION})\s+to\s+"
        rf"(?P<destination>{ROUTE_ENDPOINT_EXPRESSION_LAZY}){ROUTE_BOUNDARY}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bbetween\s+(?P<origin>{ROUTE_ENDPOINT_EXPRESSION})\s+and\s+"
        rf"(?P<destination>{ROUTE_ENDPOINT_EXPRESSION_LAZY}){ROUTE_BOUNDARY}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<origin>[A-Za-z]{3})\s+to\s+"
        r"(?P<destination>[A-Za-z]{3})" + ROUTE_BOUNDARY,
        re.IGNORECASE,
    ),
)
NO_PROGRAM_ROUTE_PATTERNS = (
    re.compile(
        r"\bfrom\s+(?P<origin>[A-Za-z]{3})\s+to\s+"
        r"(?P<destination>[A-Za-z]{3})(?=\s|[,.!?;:]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bbetween\s+(?P<origin>[A-Za-z]{3})\s+and\s+"
        r"(?P<destination>[A-Za-z]{3})(?=\s|[,.!?;:]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<origin>[A-Za-z]{3})\s+to\s+"
        r"(?P<destination>[A-Za-z]{3})(?=\s|[,.!?;:]|$)",
        re.IGNORECASE,
    ),
)
PROGRAM_SELECTION_PHRASE_RE = re.compile(
    r"\b(?:redeem|use|using|select|choose|apply|with)\s+(?:the\s+)?"
    r"(?P<candidate>[A-Za-z][A-Za-z0-9'’-]*)\b"
    r"|\b(?:book|reserve)\b[^.!?]{0,120}\b(?:using|with)\b",
    re.IGNORECASE,
)
NO_PROGRAM_PREFIX_WORDS = {
    "a", "an", "the", "this", "that", "one", "single", "adult", "adults",
    "passenger", "passengers", "traveler", "travelers", "business", "economy",
    "premium", "first", "class", "flight", "flights", "award", "awards",
    "seat", "seats", "find", "search", "look", "for", "show", "me", "check",
    "monitor", "get", "please", "availability", "one-way", "way", "trip", "with", "use",
}
NON_PROGRAM_SELECTION_WORDS = {
    "a", "an", "one", "single", "the", "this", "that", "adult", "adults",
    "passenger", "passengers", "traveler", "travelers", "traveller", "travellers",
    "seat", "seats", "business", "economy", "premium", "first", "class",
}
NO_PROGRAM_SUFFIX_WORDS = {
    "on", "for", "in", "under", "below", "at", "most", "up", "to", "maximum",
    "max", "ceiling", "next", "this", "coming", "today", "tomorrow", "one",
    "single", "a", "an", "adult", "adults", "passenger", "passengers",
    "traveler", "travelers", "traveller", "travellers", "business", "economy",
    "premium", "first", "class", "seat", "seats", "with",
}
NO_PROGRAM_ALLOWED_WORDS = frozenset(
    NO_PROGRAM_PREFIX_WORDS
    | NO_PROGRAM_SUFFIX_WORDS
    | {
        "and", "between", "book", "date", "departing", "from", "reserve",
        "route", "to", "yesterday",
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct",
        "nov", "dec",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday", "week",
    }
)
NO_PROGRAM_PUNCTUATION = frozenset(",.;:!?()")
NO_PROGRAM_WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", re.ASCII)
NO_PROGRAM_NUMBER_RE = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}|\d[\d,]*(?:\.\d+)?)", re.ASCII
)
MONTH_NAME_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)
DATE_EVIDENCE_RE = re.compile(
    rf"\b(?:\d{{4}}-\d{{2}}-\d{{2}}|"
    rf"{MONTH_NAME_PATTERN}\s+\d{{1,2}}(?:,?\s+\d{{4}})?|"
    rf"\d{{1,2}}\s+{MONTH_NAME_PATTERN}(?:\s+\d{{4}})?|"
    r"(?:next|this|coming)\s+(?:monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|week)|today|tomorrow)\b",
    re.IGNORECASE,
)
NAMED_ABSOLUTE_DATE_RE = re.compile(
    rf"\b(?:"
    rf"(?P<month_first>{MONTH_NAME_PATTERN})\s+(?P<day_first>\d{{1,2}}),?\s+(?P<year_first>\d{{4}})"
    rf"|(?P<day_second>\d{{1,2}})\s+(?P<month_second>{MONTH_NAME_PATTERN})\s+(?P<year_second>\d{{4}})"
    rf")\b",
    re.IGNORECASE,
)
MONTH_NUMBERS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}
CABIN_EVIDENCE_RE = re.compile(
    r"\b(?:premium[\s_-]+economy|economy|business|first)(?:\s+class)?\b",
    re.IGNORECASE,
)
PASSENGER_EVIDENCE_RE = re.compile(
    r"\b(?P<count>\d+|one|single|a|two|three|four|five|six|seven|eight|"
    r"nine|ten)\s+(?:adult|adults|passenger|passengers|traveler|travelers|"
    r"traveller|travellers|(?:[A-Za-z]+\s+){0,3}seat)\b",
    re.IGNORECASE,
)
PASSENGER_COUNT_WORDS = {
    "a": 1,
    "single": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


@dataclass(frozen=True)
class NormalizedCriteria:
    program: str
    origin: str
    destination: str
    departure_date: str
    cabin: str
    adults: int
    trip_type: str
    maximum_points: int


@dataclass(frozen=True)
class ParsedNaturalLanguageRequest:
    program: str
    origin: str
    destination: str
    departure_date: str
    cabin: str
    adults: int
    trip_type: str
    maximum_points: int
    missing_fields: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RequestParseResult:
    requests: List[ParsedNaturalLanguageRequest]
    program_selection: str
    stated_program: str | None = None
    clarification: str | None = None
    unsupported_reason: str | None = None
    diagnostics: Dict[str, Any] | None = None


class ParserFailure(RuntimeError):
    def __init__(self, message: str, *, diagnostics: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class AwardProviderAdapter(Protocol):
    def execute(
        self, criteria: NormalizedCriteria, atomic_task_id: str
    ) -> Dict[str, Any]:
        ...


class RequestParser(Protocol):
    def parse(
        self,
        *,
        request_id: str,
        original_text: str,
        current_date: date,
        timezone_name: str,
    ) -> RequestParseResult:
        ...


class GoogleGenAIRequestParser:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        policy: GeminiCallPolicy,
        fallback_model: str | None = None,
    ) -> None:
        if policy is None:
            raise ValueError("Google Gemini request parsing requires a caller-owned Gemini policy")
        if fallback_model is not None:
            validate_model_configuration(model, fallback_model)
        self._client = client
        self._model = model
        self._policy = policy
        self._fallback_model = fallback_model

    def parse(
        self,
        *,
        request_id: str,
        original_text: str,
        current_date: date,
        timezone_name: str,
        operation: Any | None = None,
    ) -> RequestParseResult:
        prompt = (
            "Parse this award-search request into JSON. "
            "Set program_selection to exactly omitted, supported, unsupported, or ambiguous. "
            "Return stated_program when the request names or implies a loyalty program. "
            "If the request is ambiguous, missing required fields, uses a city alias, or uses an ambiguous numeric date, "
            "return clarification or unsupported_reason instead of inventing values; for each request, list every "
            "required field that was not present in missing_fields even if a placeholder value is returned. "
            "Resolve relative dates using the provided timezone and return absolute YYYY-MM-DD dates.\n"
            f"request_id: {request_id}\n"
            f"current_date: {current_date.isoformat()}\n"
            f"timezone: {timezone_name}\n"
            f"request: {redact_sensitive_text(original_text)}\n"
        )
        active_operation = operation

        def parse_response(model: str) -> RequestParseResult:
            try:
                create_kwargs = {
                    "model": model,
                    "input": prompt,
                    "response_mime_type": "application/json",
                    "response_format": natural_language_parse_schema(),
                }
                if active_operation is not None:
                    # google-genai 1.75 exposes a per-request timeout on
                    # interactions.create; the policy remains the source of truth.
                    create_kwargs["timeout"] = max(active_operation.remaining_seconds(), 0.001)
                interaction = self._client.interactions.create(
                    **create_kwargs,
                )
                payload = json.loads("".join(
                    output.text for output in interaction.outputs or []
                    if output.type == "text"
                ))
            except json.JSONDecodeError as exc:
                raise MalformedModelOutputError("model returned invalid JSON") from exc
            except Exception:
                raise

            try:
                program_selection = payload["program_selection"]
                stated_program = payload.get("stated_program")
                requests = [
                    ParsedNaturalLanguageRequest(
                        program=str(item["program"]).strip().lower(),
                        origin=str(item["origin"]).strip().upper(),
                        destination=str(item["destination"]).strip().upper(),
                        departure_date=str(item["departure_date"]).strip(),
                        cabin=str(item["cabin"]).strip(),
                        adults=item["adults"],
                        trip_type=str(item["trip_type"]).strip().lower(),
                        maximum_points=item["maximum_points"],
                        missing_fields=item["missing_fields"],
                    )
                    for item in payload.get("requests", [])
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise MalformedModelOutputError("model returned unusable structured data") from exc

            if (
                not isinstance(program_selection, str)
                or program_selection not in PROGRAM_SELECTION_STATES
                or any(
                    not isinstance(item.missing_fields, list)
                    or any(field_name not in PARSER_REQUIRED_FIELDS for field_name in item.missing_fields)
                    for item in requests
                )
            ):
                raise MalformedModelOutputError("model returned invalid parse fields")

            return RequestParseResult(
                requests=requests,
                program_selection=program_selection,
                stated_program=stated_program,
                clarification=payload.get("clarification"),
                unsupported_reason=payload.get("unsupported_reason"),
                diagnostics={"provider": "google_genai", "model": model},
            )

        if active_operation is None:
            active_operation = self._policy.operation(
                f"request-parse:{request_id}", request_id=request_id
            )
        try:
            result = active_operation.invoke(
                model=self._model,
                purpose="request_parse",
                provider_call=parse_response,
                fallback_model=self._fallback_model,
            )
        except GeminiPolicyError as exc:
            diagnostics = dict(exc.diagnostics)
            diagnostics.update({
                "provider": "google_genai",
                "model": self._model,
                "gemini_error_classification": exc.classification.value,
                "gemini_usage": exc.diagnostics,
            })
            raise ParserFailure(redact_sensitive_text(str(exc)), diagnostics=diagnostics) from exc
        assert isinstance(result, RequestParseResult)
        diagnostics = dict(result.diagnostics or {})
        diagnostics["gemini_usage"] = active_operation.diagnostics()
        return RequestParseResult(
            requests=result.requests,
            program_selection=result.program_selection,
            stated_program=result.stated_program,
            clarification=result.clarification,
            unsupported_reason=result.unsupported_reason,
            diagnostics=diagnostics,
        )


class AeroplanFixtureAdapter:
    def execute(
        self, criteria: NormalizedCriteria, atomic_task_id: str
    ) -> Dict[str, Any]:
        from .aeroplan import AeroplanFixtureBrowser, AeroplanSearchAdapter

        fixture_name = "no_availability.html"
        if (
            criteria.origin == "JFK"
            and criteria.destination == "CDG"
            and criteria.departure_date == "2026-11-05"
            and criteria.cabin == "Business"
        ):
            fixture_name = "match.html"
        fixture = Path(__file__).parent / "fixtures" / "aeroplan" / fixture_name
        return AeroplanSearchAdapter(
            browser=AeroplanFixtureBrowser(fixture)
        ).execute(criteria, atomic_task_id)


class AnaFixtureAdapter:
    def execute(
        self, criteria: NormalizedCriteria, atomic_task_id: str
    ) -> Dict[str, str]:
        return {
            "status": "NO_AWARD_AVAILABILITY",
            "detail": f"fixture found no qualifying itinerary via {atomic_task_id}",
        }


DEFAULT_ADAPTER_REGISTRY: Mapping[str, AwardProviderAdapter] = {
    "aeroplan": AeroplanFixtureAdapter(),
    "ana": AnaFixtureAdapter(),
}


def build_request_hash(
    request_id: str,
    original_text: str,
    criteria: NormalizedCriteria,
) -> str:
    payload = {
        "request_id": request_id,
        "original_text": original_text.strip(),
        "criteria": asdict(criteria),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_request_from_parsed_natural_language(
    request_id: str,
    original_text: str,
    parsed_request: ParsedNaturalLanguageRequest,
) -> Dict[str, Any]:
    return {
        "request_id": request_id,
        "original_text": original_text,
        "program": PROGRAM_ALIASES.get(str(parsed_request.program).strip().lower(), parsed_request.program),
        "origin": parsed_request.origin,
        "destination": parsed_request.destination,
        "departure_date": parsed_request.departure_date,
        "cabin": parsed_request.cabin,
        "adults": parsed_request.adults,
        "trip_type": parsed_request.trip_type,
        "maximum_points": parsed_request.maximum_points,
    }


def build_request_set_hash(
    request_id: str,
    original_text: str,
    criteria_set: List[NormalizedCriteria],
) -> str:
    payload = {
        "request_id": request_id,
        "original_text": original_text.strip(),
        "criteria_set": [
            asdict(criteria)
            for criteria in sorted(criteria_set, key=lambda item: item.program)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deterministic_program_aliases(original_text: str) -> set[str]:
    return {
        program
        for alias, program in PROGRAM_ALIASES.items()
        if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", original_text, re.IGNORECASE)
    }


def remove_supported_program_aliases(original_text: str) -> str:
    aliases = sorted(PROGRAM_ALIASES, key=len, reverse=True)
    alias_re = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(alias) for alias in aliases) + r")(?!\w)",
        re.IGNORECASE,
    )
    return alias_re.sub(lambda match: " " * len(match.group(0)), original_text)


def extract_route_match(original_text: str) -> re.Match[str] | None:
    for pattern in ROUTE_PATTERNS:
        match = pattern.search(original_text)
        if match is not None:
            return match
    return None


def has_program_selection_phrase(original_text: str) -> bool:
    for match in PROGRAM_SELECTION_PHRASE_RE.finditer(original_text):
        candidate = match.groupdict().get("candidate")
        if candidate is None or candidate.lower() not in NON_PROGRAM_SELECTION_WORDS:
            return True
    return False


def has_safe_no_program_prefix(prefix: str) -> bool:
    words = re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", prefix.lower())
    return bool(words or not prefix.strip()) and all(
        word in NO_PROGRAM_PREFIX_WORDS for word in words
    )


def has_safe_no_program_suffix(original_text: str, route_match: re.Match[str]) -> bool:
    suffix = original_text[route_match.end():].strip()
    if not suffix:
        return True
    suffix = re.sub(r"^[,;:]+\s*", "", suffix)
    first_word = re.search(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", suffix)
    return first_word is not None and first_word.group(0).lower() in NO_PROGRAM_SUFFIX_WORDS


def _is_closed_no_program_token(
    token: str,
    token_kind: str,
    *,
    consumed_spans: set[tuple[int, int]],
    token_span: tuple[int, int],
) -> bool:
    if any(
        consumed_start <= token_span[0] and token_span[1] <= consumed_end
        for consumed_start, consumed_end in consumed_spans
    ):
        return True
    if token_kind == "word":
        return token.lower() in NO_PROGRAM_ALLOWED_WORDS
    return token in NO_PROGRAM_PUNCTUATION


def extract_closed_no_program_route(original_text: str) -> re.Match[str] | None:
    for pattern in NO_PROGRAM_ROUTE_PATTERNS:
        match = pattern.search(original_text)
        if match is not None:
            return match
    return None


def has_closed_no_program_grammar(
    original_text: str,
    route_match: re.Match[str],
) -> bool:
    """Accept only requests whose complete input is known request vocabulary."""
    consumed_spans = {route_match.span()}
    ceiling_evidence_re = re.compile(
        r"\b(?:under|below|at\s+most|up\s+to|maximum|max|ceiling)\s+"
        r"\d[\d,]*(?:\.\d+)?(?:[kK]|\s+[kK])?(?:\s+(?:points|miles))?\b"
        r"|\b\d[\d,]*(?:\.\d+)?(?:[kK]|\s+[kK])?\s+(?:points|miles)\b",
        re.IGNORECASE,
    )
    for evidence_re in (
        DATE_EVIDENCE_RE,
        CABIN_EVIDENCE_RE,
        PASSENGER_EVIDENCE_RE,
        ceiling_evidence_re,
    ):
        consumed_spans.update(match.span() for match in evidence_re.finditer(original_text))
    position = 0
    while position < len(original_text):
        whitespace = re.match(r"\s+", original_text[position:])
        if whitespace:
            position += whitespace.end()
            continue
        number = NO_PROGRAM_NUMBER_RE.match(original_text, position)
        if number:
            token_kind = "number"
            token = number.group(0)
        else:
            word = NO_PROGRAM_WORD_RE.match(original_text, position)
            if word:
                token_kind = "word"
                token = word.group(0)
            else:
                token_kind = "punctuation"
                token = original_text[position]
        if not _is_closed_no_program_token(
            token,
            token_kind,
            consumed_spans=consumed_spans,
            token_span=(position, position + len(token)),
        ):
            return False
        position += len(token)
    return True


def is_narrow_no_program_request(original_text: str) -> bool:
    if deterministic_program_aliases(original_text):
        return False
    route_match = extract_closed_no_program_route(original_text)
    if route_match is None:
        return False
    if has_program_selection_phrase(original_text):
        return False
    if not has_closed_no_program_grammar(original_text, route_match):
        return False
    return not missing_original_text_fields(original_text)


def is_narrow_supported_program_request(original_text: str) -> bool:
    """Accept explicit supported programs only when the whole input is known grammar."""
    if not deterministic_program_aliases(original_text):
        return False
    alias_free_text = remove_supported_program_aliases(original_text)
    route_match = extract_route_match(alias_free_text)
    if route_match is None:
        return False
    if not has_closed_no_program_grammar(alias_free_text, route_match):
        return False
    return not missing_original_text_fields(alias_free_text)


def has_material_program_ambiguity(original_text: str) -> bool:
    return re.search(r"\bANA\s+flights?\b", original_text, re.IGNORECASE) is not None


def validate_multi_program_criteria(
    criteria_set: List[NormalizedCriteria],
) -> str | None:
    if len(criteria_set) < 2:
        return None
    baseline = criteria_set[0]
    if any(
        any(getattr(criteria, field) != getattr(baseline, field)
            for field in SHARED_PROGRAM_CRITERIA_FIELDS)
        for criteria in criteria_set[1:]
    ):
        return (
            "all selected programs must have identical origin, destination, "
            "departure date, cabin, adults, trip type, and maximum points; "
            "only program may differ"
        )
    return None


def natural_language_parse_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "program": {"type": "string"},
                        "origin": {"type": "string"},
                        "destination": {"type": "string"},
                        "departure_date": {"type": "string"},
                        "cabin": {"type": "string"},
                        "adults": {"type": "integer"},
                        "trip_type": {"type": "string"},
                        "maximum_points": {"type": "integer"},
                        "missing_fields": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": list(PARSER_REQUIRED_FIELDS),
                            },
                        },
                    },
                    "required": [
                        "program",
                        "origin",
                        "destination",
                        "departure_date",
                        "cabin",
                        "adults",
                        "trip_type",
                        "maximum_points",
                        "missing_fields",
                    ],
                },
            },
            "program_selection": {
                "type": "string",
                "enum": ["omitted", "supported", "unsupported", "ambiguous"],
            },
            "stated_program": {"type": "string"},
            "clarification": {"type": "string"},
            "unsupported_reason": {"type": "string"},
        },
        "required": ["requests", "program_selection"],
    }


def build_confirmation_request(
    *,
    request: Dict[str, Any],
    parsed_requests: List[ParsedNaturalLanguageRequest],
    current_date: date | None = None,
    timezone_name: str = "UTC",
) -> Dict[str, Any]:
    request_id = str(request.get("request_id", "")).strip()
    original_text = str(request.get("original_text", "")).strip()
    criteria_set = [
        normalize_request(
            build_request_from_parsed_natural_language(
                request_id,
                original_text,
                parsed_request,
            ),
            current_date=current_date,
            timezone_name=timezone_name,
        )
        for parsed_request in parsed_requests
    ]
    return {
        "request_id": request_id,
        "request_hash": build_request_set_hash(request_id, original_text, criteria_set),
        "confirmed": True,
    }


def execute_confirmed_request(
    *,
    request_id: str,
    original_text: str,
    criteria: NormalizedCriteria,
    event_log_path: Path,
    adapter_registry: Mapping[str, AwardProviderAdapter],
    clock: Callable[[], datetime] | None = None,
    diagnostic_id: str | None = None,
    gemini_usage: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    request_hash = build_request_hash(request_id, original_text, criteria)
    atomic_task_id = f"{criteria.program}-{request_hash[:12]}"
    result = adapter_registry[criteria.program].execute(criteria, atomic_task_id)
    extra_fields: Dict[str, Any] = {
        key: value for key, value in result.items() if key not in {"status", "detail"}
    }
    if diagnostic_id:
        extra_fields["diagnostic_id"] = diagnostic_id
    if gemini_usage is not None:
        extra_fields["gemini_usage"] = gemini_usage
    return emit_event(
        event_log_path=event_log_path,
        request_id=request_id,
        original_text=original_text,
        atomic_task_id=atomic_task_id,
        normalized_criteria=asdict(criteria),
        status=result["status"],
        detail=result["detail"],
        clock=clock,
        extra_fields=extra_fields or None,
    )


def run_structured_request(
    *,
    request: Dict[str, Any],
    confirmation: Dict[str, Any],
    event_log_path: Path,
    adapter_registry: Mapping[str, AwardProviderAdapter] = DEFAULT_ADAPTER_REGISTRY,
    current_date: date | None = None,
    timezone_name: str = "UTC",
    clock: Callable[[], datetime] | None = None,
) -> Dict[str, Any]:
    request_id = str(request.get("request_id", "")).strip()
    original_text = str(request.get("original_text", "")).strip()
    try:
        request_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return emit_event(
            event_log_path=event_log_path,
            request_id=request_id,
            original_text=original_text,
            atomic_task_id=None,
            normalized_criteria=None,
            status="UNSUPPORTED_REQUEST",
            detail="timezone must be a valid IANA timezone",
            clock=clock,
        )
    if current_date is None:
        now = clock() if clock is not None else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        current_date = now.astimezone(request_timezone).date()
    try:
        criteria = normalize_request(
            request,
            current_date=current_date,
            adapter_registry=adapter_registry,
        )
    except ValueError as exc:
        return emit_event(
            event_log_path=event_log_path,
            request_id=request_id,
            original_text=original_text,
            atomic_task_id=None,
            normalized_criteria=None,
            status="UNSUPPORTED_REQUEST",
            detail=str(exc),
            clock=clock,
        )

    request_hash = build_request_hash(request_id, original_text, criteria)
    confirmation_error = validate_confirmation(confirmation, request_id, request_hash)
    if confirmation_error is not None:
        return emit_event(
            event_log_path=event_log_path,
            request_id=request_id,
            original_text=original_text,
            atomic_task_id=None,
            normalized_criteria=asdict(criteria),
            status="CONFIRMATION_REQUIRED",
            detail=confirmation_error,
            clock=clock,
        )

    return execute_confirmed_request(
        request_id=request_id,
        original_text=original_text,
        criteria=criteria,
        event_log_path=event_log_path,
        adapter_registry=adapter_registry,
        clock=clock,
    )


def run_request(
    *,
    request: Dict[str, Any],
    confirmation: Dict[str, Any],
    event_log_path: Path,
    parser: RequestParser | None = None,
    adapter_registry: Mapping[str, AwardProviderAdapter] = DEFAULT_ADAPTER_REGISTRY,
    current_date: date | None = None,
    timezone_name: str = "UTC",
    clock: Callable[[], datetime] | None = None,
    gemini_policy: GeminiCallPolicy | None = None,
    gemini_operation: Any | None = None,
    gemini_model: str | None = None,
    fallback_gemini_model: str | None = None,
) -> List[Dict[str, Any]]:
    if "program" in request:
        return [
            run_structured_request(
                request=request,
                confirmation=confirmation,
                event_log_path=event_log_path,
                adapter_registry=adapter_registry,
                current_date=current_date,
                timezone_name=timezone_name,
                clock=clock,
            )
        ]

    request_id = str(request.get("request_id", "")).strip()
    original_text = str(request.get("original_text", "")).strip()
    diagnostic_id = uuid4().hex
    gemini_usage: Dict[str, Any] | None = None

    def diagnostic(metadata: Dict[str, Any], error: str | None = None) -> None:
        metadata = redact_sensitive(metadata)
        error = redact_sensitive_text(error) if error is not None else None
        try:
            json.dumps(metadata)
        except (TypeError, ValueError):
            metadata = {"diagnostic_metadata_error": type(metadata).__name__}
        append_event(event_log_path.with_suffix(".diagnostics.jsonl"), {
            "diagnostic_id": diagnostic_id, "request_id": request_id,
            "original_text": redact_sensitive_text(original_text),
            "metadata": metadata, "error": error,
        })

    def report(status: str, detail: str, criteria=None, **fields) -> List[Dict[str, Any]]:
        if gemini_usage is not None:
            fields["gemini_usage"] = gemini_usage
        return [emit_event(
            event_log_path=event_log_path, request_id=request_id,
            original_text=original_text, atomic_task_id=None,
            normalized_criteria=criteria, status=status, detail=detail, clock=clock,
            extra_fields={"diagnostic_id": diagnostic_id, **fields},
        )]

    try:
        request_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        diagnostic({}, str(exc))
        return report("UNSUPPORTED_REQUEST", "timezone must be a valid IANA timezone")

    if current_date is None:
        now = clock() if clock is not None else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        current_date = now.astimezone(request_timezone).date()

    try:
        if parser is None:
            if gemini_policy is None:
                gemini_policy = GeminiCallPolicy(
                    db_path=event_log_path.with_suffix(".gemini.sqlite3"),
                    config=GeminiPolicyConfig(
                        timezone_name=timezone_name,
                    ),
                    defer_initialization=True,
                )
            if gemini_operation is None:
                gemini_operation = gemini_policy.operation(
                    f"request-parse:{request_id}", request_id=request_id
                )
            if hasattr(gemini_operation, "diagnostics"):
                gemini_usage = gemini_operation.diagnostics(include_daily_reads=False)
            gemini_policy.initialize(gemini_operation)
            if hasattr(gemini_operation, "diagnostics"):
                gemini_usage = gemini_operation.diagnostics(include_daily_reads=False)
            parser = build_default_request_parser(
                model=gemini_model,
                fallback_model=fallback_gemini_model,
                policy=gemini_policy,
                operation=gemini_operation,
            )
        elif gemini_operation is None and isinstance(parser, GoogleGenAIRequestParser):
            gemini_operation = gemini_policy.operation(
                f"request-parse:{request_id}", request_id=request_id
            ) if gemini_policy is not None else None
        if gemini_policy is not None and gemini_operation is not None and hasattr(gemini_operation, "diagnostics"):
            gemini_usage = gemini_operation.diagnostics(include_daily_reads=False)
        parse_kwargs = {
            "request_id": request_id, "original_text": original_text,
            "current_date": current_date, "timezone_name": timezone_name,
        }
        parse_parameters = inspect.signature(parser.parse).parameters
        if "operation" in parse_parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parse_parameters.values()
        ):
            parse_kwargs["operation"] = gemini_operation
        parse_result = parser.parse(**parse_kwargs)
        if (
            not isinstance(parse_result, RequestParseResult)
            or not isinstance(parse_result.requests, list)
            or any(not isinstance(item, ParsedNaturalLanguageRequest) for item in parse_result.requests)
            or any(
                not isinstance(item.missing_fields, list)
                or len(set(item.missing_fields)) != len(item.missing_fields)
                or any(
                    type(field_name) is not str
                    or field_name not in PARSER_REQUIRED_FIELDS
                    for field_name in item.missing_fields
                )
                for item in parse_result.requests
            )
            or any(value is not None and (not isinstance(value, str) or not value.strip())
                   for value in (parse_result.clarification, parse_result.unsupported_reason))
            or (parse_result.diagnostics is not None and not isinstance(parse_result.diagnostics, dict))
        ):
            raise ParserFailure("parser returned invalid structured data")
        if parse_result.program_selection not in PROGRAM_SELECTION_STATES:
            raise ParserFailure("parser returned invalid program selection state")
        if parse_result.stated_program is not None and (
            not isinstance(parse_result.stated_program, str)
            or not parse_result.stated_program.strip()
        ):
            raise ParserFailure("parser returned invalid stated program")
    except Exception as exc:
        metadata = getattr(exc, "diagnostics", {"parser_exception": type(exc).__name__})
        if isinstance(metadata, dict):
            if metadata.get("gemini_usage") is not None:
                gemini_usage = metadata["gemini_usage"]
            classification = metadata.get("gemini_error_classification")
        else:
            classification = None
        if classification is None and isinstance(exc, GeminiPolicyError):
            classification = exc.classification.value
            gemini_usage = exc.diagnostics
        diagnostic(redact_sensitive(metadata), redact_sensitive_text(str(exc)))
        if classification in {
            GeminiErrorClassification.RUN_BUDGET_EXHAUSTION.value,
            GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION.value,
        }:
            status = (
                "GEMINI_BUDGET_EXHAUSTED"
                if classification == GeminiErrorClassification.RUN_BUDGET_EXHAUSTION.value
                else "GEMINI_QUOTA_EXHAUSTED"
            )
            detail = (
                "Gemini call allowance exhausted; no model call was started"
                if not gemini_usage or gemini_usage.get("attempted_calls", 0) == 0
                else "Gemini call allowance exhausted; no additional model call was started"
            )
            return report(status, detail)
        if classification is not None:
            return report("GEMINI_FAILED", "Gemini provider call failed; see diagnostics")
        return report("PARSER_FAILED", "request parsing failed; see diagnostics")

    diagnostic(parse_result.diagnostics or {})
    if parse_result.diagnostics:
        gemini_usage = parse_result.diagnostics.get("gemini_usage")
    missing_fields = sorted({
        field_name
        for item in parse_result.requests
        for field_name in item.missing_fields
    })
    if missing_fields:
        return report(
            "CLARIFICATION_REQUIRED",
            "parser marked required fields missing: " + ", ".join(missing_fields),
        )
    if parse_result.program_selection == "unsupported":
        return report(
            "UNSUPPORTED_REQUEST",
            parse_result.unsupported_reason
            or f"program is not supported: {parse_result.stated_program or 'stated program'}",
        )
    if parse_result.program_selection == "ambiguous":
        return report(
            "CLARIFICATION_REQUIRED",
            parse_result.clarification
            or (
                f"program selection is ambiguous ({parse_result.stated_program}); "
                "specify one supported loyalty program"
                if parse_result.stated_program
                else "program selection is ambiguous; specify one supported loyalty program"
            ),
        )
    if has_material_program_ambiguity(original_text):
        return report(
            "CLARIFICATION_REQUIRED",
            "ANA flight is ambiguous: specify ANA Mileage Club or an operating carrier",
        )
    if re.search(r"(?<![\d./-])\d{1,2}([./-])\d{1,2}(?:\1\d{2,4})?(?![\d./-])", original_text):
        return report("CLARIFICATION_REQUIRED", "numeric date is ambiguous; specify YYYY-MM-DD or a named month")
    if parse_result.unsupported_reason is not None:
        return report("UNSUPPORTED_REQUEST", parse_result.unsupported_reason)
    if parse_result.clarification is not None:
        return report("CLARIFICATION_REQUIRED", parse_result.clarification)

    if not parse_result.requests and parse_result.program_selection == "omitted":
        return report("PARSER_FAILED", "parser returned no executable request")
    stated_programs = deterministic_program_aliases(original_text)
    if parse_result.program_selection == "omitted":
        if parse_result.stated_program or not is_narrow_no_program_request(original_text):
            return report(
                "CLARIFICATION_REQUIRED",
                "program selection provenance is omitted, but the original request is not "
                "a complete unambiguous no-program request",
            )
        expected_programs = {"aeroplan"}
    else:
        if not stated_programs:
            return report(
                "CLARIFICATION_REQUIRED",
                "supported program selection has no deterministic supported alias",
            )
        expected_programs = stated_programs
        if parse_result.stated_program and (
            deterministic_program_aliases(parse_result.stated_program) != expected_programs
        ):
            return report(
                "CLARIFICATION_REQUIRED",
                "supported program selection does not match the stated program",
            )
    if not parse_result.requests:
        return report("PARSER_FAILED", "parser returned no executable request")
    if parse_result.program_selection != "omitted":
        raw_programs = {
            PROGRAM_ALIASES.get(str(item.program).strip().lower(), str(item.program).strip().lower())
            for item in parse_result.requests
        }
        if raw_programs != expected_programs:
            return report("CLARIFICATION_REQUIRED", "parsed programs do not match the stated program selection")
    original_text_missing = missing_original_text_fields(original_text)
    if original_text_missing:
        labels = [
            f"{field} (points ceiling)" if field == "maximum_points" else field
            for field in sorted(original_text_missing)
        ]
        return report(
            "CLARIFICATION_REQUIRED",
            "original request lacks independent evidence for required fields: "
            + ", ".join(labels),
        )
    if parse_result.program_selection != "omitted" \
            and not is_narrow_supported_program_request(original_text):
        return report(
            "CLARIFICATION_REQUIRED",
            "original request contains unsupported or unrecognized instructions",
        )
    parsed_requests = parse_result.requests
    binding_error = semantic_binding_error(
        original_text, parsed_requests, fields={"adults"}
    )
    if binding_error is not None:
        return report("CLARIFICATION_REQUIRED", binding_error)
    if parse_result.program_selection == "omitted":
        parsed_requests = [replace(item, program="aeroplan") for item in parsed_requests]

    try:
        normalized_requests = [
            normalize_request(
                build_request_from_parsed_natural_language(request_id, original_text, parsed_request),
                current_date=current_date, adapter_registry=adapter_registry,
            )
            for parsed_request in parsed_requests
        ]
    except ValueError as exc:
        return report("UNSUPPORTED_REQUEST", str(exc))
    if {item.program for item in normalized_requests} != expected_programs:
        return report("CLARIFICATION_REQUIRED", "parsed programs do not match the stated program selection")
    if len({item.program for item in normalized_requests}) != len(normalized_requests):
        return report("CLARIFICATION_REQUIRED", "specify exactly one atomic task per program")
    ceilings, ceiling_syntax_valid = extract_points_ceilings(original_text)
    if (
        not ceiling_syntax_valid
        or len(ceilings) != 1
        or {item.maximum_points for item in normalized_requests} != ceilings
    ):
        return report("CLARIFICATION_REQUIRED", "specify one shared numeric points ceiling for every program")
    criteria_consistency_error = validate_multi_program_criteria(normalized_requests)
    if criteria_consistency_error is not None:
        return report("CLARIFICATION_REQUIRED", criteria_consistency_error)
    binding_error = semantic_binding_error(original_text, normalized_requests)
    if binding_error is not None:
        return report("CLARIFICATION_REQUIRED", binding_error)
    request_hash = build_request_set_hash(request_id, original_text, normalized_requests)
    confirmation_error = validate_confirmation(confirmation, request_id, request_hash)
    criteria_set = [asdict(criteria) for criteria in normalized_requests]
    airport_expansions = {
        field: criteria_set[0][field] for field in ("origin", "destination")
        if not re.search(r"\b" + criteria_set[0][field] + r"\b", original_text, re.IGNORECASE)
    }
    if airport_expansions and (
        confirmation_error is not None or confirmation.get("airport_expansions") != airport_expansions
    ):
        return report(
            "CLARIFICATION_REQUIRED", "explicitly confirm the proposed airport expansion",
            criteria_set[0] if len(criteria_set) == 1 else None,
            request_hash=request_hash, normalized_criteria_set=criteria_set,
            airport_expansions=airport_expansions,
        )
    if confirmation_error is not None:
        return report(
            "CONFIRMATION_REQUIRED", confirmation_error,
            criteria_set[0] if len(criteria_set) == 1 else None,
            request_hash=request_hash, normalized_criteria_set=criteria_set,
        )

    return [
        execute_confirmed_request(
            request_id=request_id, original_text=original_text, criteria=criteria,
            event_log_path=event_log_path, adapter_registry=adapter_registry,
            clock=clock, diagnostic_id=diagnostic_id, gemini_usage=gemini_usage,
        )
        for criteria in normalized_requests
    ]


def normalize_request(
    request: Dict[str, Any],
    *,
    current_date: date | None = None,
    timezone_name: str = "UTC",
    adapter_registry: Mapping[str, AwardProviderAdapter] = DEFAULT_ADAPTER_REGISTRY,
) -> NormalizedCriteria:
    request_id = str(request.get("request_id", "")).strip()
    if not request_id:
        raise ValueError("request_id is required")
    original_text = str(request.get("original_text", "")).strip()
    if not original_text:
        raise ValueError("original_text is required")

    program = str(request.get("program", "")).strip().lower()
    if program not in adapter_registry:
        raise ValueError("program is not supported")

    origin = normalize_airport(request.get("origin"), "origin")
    destination = normalize_airport(request.get("destination"), "destination")
    departure_date = normalize_departure_date(
        request.get("departure_date"),
        current_date=current_date,
        timezone_name=timezone_name,
    )
    cabin = normalize_cabin(request.get("cabin"))
    adults = request.get("adults")
    if type(adults) is not int or adults != 1:
        raise ValueError("adults must be exactly 1")
    trip_type = str(request.get("trip_type", "")).strip().lower()
    if trip_type != "one_way":
        raise ValueError("trip_type must be exactly 'one_way'")
    maximum_points = request.get("maximum_points")
    if not isinstance(maximum_points, int) or isinstance(maximum_points, bool) or maximum_points <= 0:
        raise ValueError("maximum_points must be a positive integer")

    return NormalizedCriteria(
        program=program,
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        cabin=cabin,
        adults=adults,
        trip_type=trip_type,
        maximum_points=maximum_points,
    )


@lru_cache(maxsize=1)
def _airport_iata_codes() -> frozenset[str]:
    from airportsdata import load

    records = load("IATA")
    return frozenset(
        code
        for code in records
        if isinstance(code, str)
        and re.fullmatch(r"[A-Z]{3}", code, flags=re.ASCII) is not None
    )


def normalize_airport(value: Any, field_name: str) -> str:
    airport = str(value or "").strip().upper()
    if (
        re.fullmatch(r"[A-Z]{3}", airport, flags=re.ASCII) is None
        or airport not in _airport_iata_codes()
    ):
        raise ValueError(f"{field_name} must be a supported three-letter IATA code")
    return airport


def normalize_departure_date(
    value: Any,
    *,
    current_date: date | None = None,
    timezone_name: str = "UTC",
) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw, flags=re.ASCII) is None:
        raise ValueError("departure_date must be an exact YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("departure_date must be an exact YYYY-MM-DD date") from exc
    if current_date is None:
        try:
            current_date = datetime.now(ZoneInfo(timezone_name)).date()
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
    if parsed < current_date:
        raise ValueError("departure_date cannot be in the past")
    return parsed.isoformat()


def normalize_cabin(value: Any) -> str:
    cabin_key = str(value or "").strip().lower().replace(" ", "_")
    if cabin_key not in SUPPORTED_CABINS:
        raise ValueError("cabin must be Economy, Premium Economy, Business, or First")
    return SUPPORTED_CABINS[cabin_key]


def extract_points_ceilings(original_text: str) -> tuple[set[int], bool]:
    tokens = re.findall(
        r"\b(?:under|below|at\s+most|up\s+to|maximum|max|ceiling)\s+"
        r"([^\s.;!?]+(?:\s+[kK])?)"
        r"|\b([^\s.;!?]+(?:\s+[kK])?)\s*(?:points|miles)\b",
        original_text,
        flags=re.IGNORECASE,
    )
    ceilings: set[int] = set()
    for prefixed, suffixed in tokens:
        token = prefixed or suffixed
        if suffixed and not re.match(r"[+-]?\d", token):
            continue
        ceiling = parse_points_ceiling_token(token)
        if ceiling is None:
            return set(), False
        ceilings.add(ceiling)
    return ceilings, True


def missing_original_text_fields(original_text: str) -> set[str]:
    missing: set[str] = set()
    route_match = extract_route_match(original_text)
    if route_match is None:
        missing.update(("origin", "destination"))
    if DATE_EVIDENCE_RE.search(original_text) is None:
        missing.add("departure_date")
    if CABIN_EVIDENCE_RE.search(original_text) is None:
        missing.add("cabin")
    if PASSENGER_EVIDENCE_RE.search(original_text) is None:
        missing.add("adults")
    ceilings, syntax_valid = extract_points_ceilings(original_text)
    if not syntax_valid or len(ceilings) != 1:
        missing.add("maximum_points")
    return missing


def _exact_iata_route_evidence(original_text: str) -> tuple[str, str] | None:
    route_match = extract_route_match(original_text)
    if route_match is None:
        return None
    origin = route_match.group("origin").strip()
    destination = route_match.group("destination").strip()
    if not all(re.fullmatch(r"[A-Za-z]{3}", value, flags=re.ASCII)
               for value in (origin, destination)):
        return None
    return origin.upper(), destination.upper()


def _stated_passenger_counts(original_text: str) -> set[int]:
    counts: set[int] = set()
    for match in PASSENGER_EVIDENCE_RE.finditer(original_text):
        raw_count = match.group("count").lower()
        if raw_count.isdigit():
            counts.add(int(raw_count))
        elif raw_count in PASSENGER_COUNT_WORDS:
            counts.add(PASSENGER_COUNT_WORDS[raw_count])
    return counts


def _stated_named_absolute_dates(original_text: str) -> tuple[set[str], bool, bool]:
    dates: set[str] = set()
    saw_date = False
    syntax_valid = True
    for match in NAMED_ABSOLUTE_DATE_RE.finditer(original_text):
        saw_date = True
        if match.group("month_first") is not None:
            month_name = match.group("month_first")
            day = match.group("day_first")
            year = match.group("year_first")
        else:
            month_name = match.group("month_second")
            day = match.group("day_second")
            year = match.group("year_second")
        try:
            dates.add(date(
                int(year), MONTH_NUMBERS[month_name.lower()], int(day)
            ).isoformat())
        except (KeyError, TypeError, ValueError):
            syntax_valid = False
    return dates, saw_date, syntax_valid


def semantic_binding_error(
    original_text: str,
    parsed_requests: List[Any],
    *,
    fields: set[str] | None = None,
) -> str | None:
    """Reject parser values that disagree with deterministic text evidence."""
    if fields is None:
        fields = {
            "origin", "destination", "departure_date", "cabin", "adults", "maximum_points"
        }
    exact_route = _exact_iata_route_evidence(original_text)
    exact_dates = {
        match.group(0)
        for match in re.finditer(r"\b\d{4}-\d{2}-\d{2}\b", original_text)
    }
    named_dates, named_date_evidence, named_date_syntax_valid = _stated_named_absolute_dates(
        original_text
    )
    absolute_dates = exact_dates | named_dates
    stated_cabins = {
        re.sub(r"[\s_-]+class$", "", match.group(0), flags=re.IGNORECASE)
        .replace("-", " ").replace("_", " ").strip().lower()
        for match in CABIN_EVIDENCE_RE.finditer(original_text)
    }
    stated_adults = _stated_passenger_counts(original_text)
    ceilings, ceiling_syntax_valid = extract_points_ceilings(original_text)

    mismatches: set[str] = set()
    for parsed_request in parsed_requests:
        if exact_route is not None and {"origin", "destination"} & fields:
            parsed_route = (
                str(parsed_request.origin).strip().upper(),
                str(parsed_request.destination).strip().upper(),
            )
            if all(re.fullmatch(r"[A-Z]{3}", value, flags=re.ASCII) for value in parsed_route) \
                    and parsed_route != exact_route:
                mismatches.update(("origin", "destination"))
        parsed_date = str(parsed_request.departure_date).strip()
        if "departure_date" in fields and (exact_dates or named_date_evidence) \
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parsed_date) \
                and (
                    not named_date_syntax_valid
                    or len(absolute_dates) != 1
                    or parsed_date not in absolute_dates
                ):
            mismatches.add("departure_date")
        if "cabin" in fields and stated_cabins:
            try:
                parsed_cabin = normalize_cabin(parsed_request.cabin).lower()
            except ValueError:
                parsed_cabin = None
            if parsed_cabin is not None \
                    and (len(stated_cabins) != 1 or parsed_cabin not in stated_cabins):
                mismatches.add("cabin")
        if "adults" in fields and stated_adults and type(parsed_request.adults) is int \
                and (len(stated_adults) != 1 or parsed_request.adults not in stated_adults):
            mismatches.add("adults")
        if "maximum_points" in fields and ceiling_syntax_valid and len(ceilings) == 1 \
                and type(parsed_request.maximum_points) is int \
                and parsed_request.maximum_points not in ceilings:
            mismatches.add("maximum_points")

    if not mismatches:
        return None
    labels = [
        "maximum_points (points ceiling)" if field == "maximum_points" else field
        for field in sorted(mismatches)
    ]
    return "parsed values do not match original text evidence: " + ", ".join(labels)


def parse_points_ceiling_token(token: str) -> int | None:
    token = re.sub(r"\s+", "", token)
    if re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:k)?", token, flags=re.IGNORECASE) is None:
        return None
    compact = token.replace(",", "").lower()
    multiplier = 1000 if compact.endswith("k") else 1
    digits = compact[:-1] if multiplier == 1000 else compact
    try:
        return int(digits) * multiplier
    except (TypeError, ValueError, OverflowError):
        return None


def validate_confirmation(
    confirmation: Dict[str, Any],
    request_id: str,
    request_hash: str,
) -> str | None:
    if confirmation.get("confirmed") is not True:
        return "explicit confirmation is required"
    if str(confirmation.get("request_id", "")).strip() != request_id:
        return "confirmation request_id does not match"
    if str(confirmation.get("request_hash", "")).strip() != request_hash:
        return "confirmation request_hash does not match the current request"
    return None


def emit_event(
    *,
    event_log_path: Path,
    request_id: str,
    original_text: str,
    atomic_task_id: str | None,
    normalized_criteria: Dict[str, Any] | None,
    status: str,
    detail: str,
    clock: Callable[[], datetime] | None = None,
    extra_fields: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    now = clock() if clock is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    timestamp = now.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    event = {
        "timestamp": timestamp,
        "request_id": request_id,
        "original_text": redact_sensitive_text(original_text),
        "atomic_task_id": atomic_task_id,
        "normalized_criteria": normalized_criteria,
        "status": status,
        "detail": detail,
    }
    if normalized_criteria is not None:
        event["program"] = normalized_criteria["program"]
    if extra_fields:
        event.update(extra_fields)

    append_event(event_log_path, event)

    print(render_terminal_report(event))
    return event


def append_event(event_log_path: Path, event: Dict[str, Any]) -> None:
    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    record = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
    with event_log_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() > 0:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    record = b"\n" + record
            remaining = memoryview(record)
            while remaining:
                written = handle.write(remaining)
                if written is None or written <= 0:
                    raise OSError("incomplete JSONL event write")
                remaining = remaining[written:]
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def render_terminal_report(event: Dict[str, Any]) -> str:
    criteria_set = event.get("normalized_criteria_set") or [event.get("normalized_criteria")]
    criteria_report = ""
    for criteria in filter(None, criteria_set):
        criteria_report += (
            f" program={criteria['program']} origin={criteria['origin']}"
            f" destination={criteria['destination']} departure_date={criteria['departure_date']}"
            f" cabin={criteria['cabin']} adults={criteria['adults']}"
            f" trip_type={criteria['trip_type']} maximum_points={criteria['maximum_points']}"
        )
    task = event["atomic_task_id"] or "none"
    search_entry = (
        f" search_entry_url={event['search_entry_url']}"
        if event.get("search_entry_url") else ""
    )
    return (
        f"{event['status']} request={event['request_id']}"
        f" original_text={redact_sensitive_text(str(event['original_text']))} task={task}"
        f"{criteria_report}{search_entry} detail={event['detail']} at {event['timestamp']}"
    )


def build_default_request_parser(
    model: str | None = None,
    *,
    policy: GeminiCallPolicy,
    fallback_model: str | None = None,
    operation: Any | None = None,
) -> GoogleGenAIRequestParser:
    if policy is None:
        raise ValueError("Google Gemini request parsing requires a caller-owned Gemini policy")
    primary_model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    fallback = fallback_model or os.environ.get("FALLBACK_GEMINI_MODEL", "gemini-3.6-flash")
    validate_model_configuration(primary_model, fallback)
    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError("google-genai is required for natural-language requests") from exc
    from google.genai import types

    def construct_client() -> Any:
        return genai.Client(
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=1),
            )
        )

    if operation is not None:
        policy.initialize(operation)
        try:
            client = operation.run_sync(construct_client)
        except TimeoutError as exc:
            raise GeminiCallFailure(
                "Gemini operation deadline exhausted during client setup",
                classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                diagnostics=operation.diagnostics(
                    terminal_classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                    terminal_error="operation deadline exhausted during client setup",
                    include_daily_reads=False,
                ),
            ) from exc
    else:
        client = construct_client()
    return GoogleGenAIRequestParser(
        client=client,
        model=primary_model,
        policy=policy,
        fallback_model=fallback,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a confirmed award request.")
    parser.add_argument("--request", required=True, help="Path to the structured JSON request file.")
    parser.add_argument("--confirmation", required=True, help="Path to the structured JSON confirmation file.")
    parser.add_argument("--event-log", required=True, help="Path to the append-only JSONL event log.")
    parser.add_argument(
        "--current-date",
        type=date.fromisoformat,
        help="Override today's date for deterministic fixture/demo runs (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--timezone",
        default=os.environ.get("REQUEST_TIMEZONE", "UTC"),
        help="IANA timezone used to resolve relative natural-language dates.",
    )
    parser.add_argument("--gemini-model", default=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
    parser.add_argument(
        "--fallback-gemini-model",
        default=os.environ.get("FALLBACK_GEMINI_MODEL", "gemini-3.6-flash"),
    )
    parser.add_argument(
        "--gemini-run-call-limit",
        type=int,
        default=int(os.environ.get("GEMINI_RUN_CALL_LIMIT", "3")),
    )
    parser.add_argument(
        "--gemini-usage-db-path",
        default=os.environ.get("GEMINI_USAGE_DB_PATH", ".artifacts/gemini-usage.sqlite3"),
    )
    parser.add_argument(
        "--gemini-daily-call-limit",
        dest="gemini_daily_call_limits",
        action="append",
        default=None,
        metavar="MODEL=LIMIT",
        help="Override one model's persistent daily Gemini allowance; repeat as needed.",
    )
    parser.add_argument(
        "--gemini-timezone",
        default=os.environ.get("GEMINI_TIMEZONE", "UTC"),
        help="IANA timezone used for persistent Gemini daily counters.",
    )
    parser.add_argument(
        "--gemini-max-attempts",
        type=int,
        default=int(os.environ.get("GEMINI_MAX_ATTEMPTS", "2")),
    )
    parser.add_argument(
        "--gemini-retry-backoff-seconds",
        type=lambda value: tuple(float(item.strip()) for item in value.split(",") if item.strip()),
        default=tuple(
            float(item.strip())
            for item in os.environ.get("GEMINI_RETRY_BACKOFF_SECONDS", "0.25").split(",")
            if item.strip()
        ),
    )
    parser.add_argument(
        "--gemini-operation-deadline-seconds",
        type=float,
        default=float(os.environ.get("GEMINI_OPERATION_DEADLINE_SECONDS", "60")),
    )
    args = parser.parse_args()
    raw_limits = args.gemini_daily_call_limits
    if raw_limits is None:
        raw_limits = [os.environ.get("GEMINI_DAILY_CALL_LIMITS", "")]
    limits: dict[str, int] = {}
    for item in ",".join(raw_limits).split(","):
        if not item.strip():
            continue
        model, separator, raw_limit = item.partition("=")
        if not separator or not model.strip():
            parser.error("daily Gemini limits must use MODEL=LIMIT entries")
        try:
            limits[model.strip()] = int(raw_limit.strip())
        except ValueError:
            parser.error("daily Gemini limits must use integer MODEL=LIMIT entries")
    args.gemini_daily_call_limits = limits
    return args


def load_json(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    request = load_json(args.request)
    gemini_policy = None
    if "program" not in request:
        validate_model_configuration(args.gemini_model, args.fallback_gemini_model)
        gemini_policy = GeminiCallPolicy(
            db_path=Path(args.gemini_usage_db_path),
            config=GeminiPolicyConfig(
                run_call_limit=args.gemini_run_call_limit,
                daily_call_limits={
                    **DEFAULT_DAILY_CALL_LIMITS,
                    **args.gemini_daily_call_limits,
                },
                timezone_name=args.gemini_timezone,
                max_attempts=args.gemini_max_attempts,
                retry_backoff_seconds=args.gemini_retry_backoff_seconds,
                operation_deadline_seconds=args.gemini_operation_deadline_seconds,
            ),
            defer_initialization=True,
        )
    events = run_request(
        request=request,
        confirmation=load_json(args.confirmation),
        event_log_path=Path(args.event_log),
        current_date=args.current_date,
        timezone_name=args.timezone,
        gemini_policy=gemini_policy,
        gemini_model=args.gemini_model if gemini_policy is not None else None,
        fallback_gemini_model=args.fallback_gemini_model if gemini_policy is not None else None,
    )
    return 0 if all(event["status"] == "MATCH_FOUND" for event in events) else 1


if __name__ == "__main__":
    raise SystemExit(main())
