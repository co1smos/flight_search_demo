from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Protocol

SUPPORTED_CABINS = {
    "economy": "Economy",
    "premium_economy": "Premium Economy",
    "business": "Business",
    "first": "First",
}
SUPPORTED_AIRPORTS = {"CDG", "JFK"}


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


@dataclass(frozen=True)
class RequestParseResult:
    requests: List[ParsedNaturalLanguageRequest]
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
    ) -> Dict[str, str]:
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
    def __init__(self, *, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    def parse(
        self,
        *,
        request_id: str,
        original_text: str,
        current_date: date,
        timezone_name: str,
    ) -> RequestParseResult:
        prompt = (
            "Parse this award-search request into JSON. "
            "If the request is ambiguous, missing required fields, uses a city alias, or uses an ambiguous numeric date, "
            "return clarification or unsupported_reason instead of inventing values. "
            "Resolve relative dates using the provided timezone and return absolute YYYY-MM-DD dates.\n"
            f"request_id: {request_id}\n"
            f"current_date: {current_date.isoformat()}\n"
            f"timezone: {timezone_name}\n"
            f"request: {original_text}\n"
        )
        try:
            interaction = self._client.interactions.create(
                model=self._model,
                input=prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": natural_language_parse_schema(),
                },
            )
            payload = json.loads(interaction.output_text)
        except Exception as exc:
            raise ParserFailure(
                str(exc),
                diagnostics={"provider": "google_genai", "model": self._model},
            ) from exc

        try:
            requests = [
                ParsedNaturalLanguageRequest(
                    program=str(item["program"]).strip().lower(),
                    origin=str(item["origin"]).strip().upper(),
                    destination=str(item["destination"]).strip().upper(),
                    departure_date=str(item["departure_date"]).strip(),
                    cabin=str(item["cabin"]).strip(),
                    adults=int(item["adults"]),
                    trip_type=str(item["trip_type"]).strip().lower(),
                    maximum_points=int(item["maximum_points"]),
                )
                for item in payload.get("requests", [])
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ParserFailure(
                "parser returned invalid structured data",
                diagnostics={"provider": "google_genai", "model": self._model},
            ) from exc

        diagnostics = {"provider": "google_genai", "model": self._model}
        return RequestParseResult(
            requests=requests,
            clarification=payload.get("clarification"),
            unsupported_reason=payload.get("unsupported_reason"),
            diagnostics=diagnostics,
        )


class AeroplanFixtureAdapter:
    def execute(
        self, criteria: NormalizedCriteria, atomic_task_id: str
    ) -> Dict[str, str]:
        if (
            criteria.origin == "JFK"
            and criteria.destination == "CDG"
            and criteria.departure_date == "2026-11-05"
            and criteria.cabin == "Business"
        ):
            if criteria.maximum_points < 60000:
                return {
                    "status": "ABOVE_POINTS_LIMIT",
                    "detail": f"fixture award costs 60000 points via {atomic_task_id}",
                }
            return {
                "status": "MATCH_FOUND",
                "detail": f"fixture matched confirmed request via {atomic_task_id}",
            }
        return {
            "status": "NO_AWARD_AVAILABILITY",
            "detail": f"fixture found no qualifying itinerary via {atomic_task_id}",
        }


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
        "program": parsed_request.program,
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
                    ],
                },
            },
            "clarification": {"type": "string"},
            "unsupported_reason": {"type": "string"},
        },
        "required": ["requests"],
    }


def build_confirmation_request(
    *,
    request: Dict[str, Any],
    parsed_requests: List[ParsedNaturalLanguageRequest],
) -> Dict[str, Any]:
    request_id = str(request.get("request_id", "")).strip()
    original_text = str(request.get("original_text", "")).strip()
    criteria_set = [
        normalize_request(
            build_request_from_parsed_natural_language(
                request_id,
                original_text,
                parsed_request,
            )
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
) -> Dict[str, Any]:
    request_hash = build_request_hash(request_id, original_text, criteria)
    atomic_task_id = f"{criteria.program}-{request_hash[:12]}"
    result = adapter_registry[criteria.program].execute(criteria, atomic_task_id)
    return emit_event(
        event_log_path=event_log_path,
        request_id=request_id,
        original_text=original_text,
        atomic_task_id=atomic_task_id,
        normalized_criteria=asdict(criteria),
        status=result["status"],
        detail=result["detail"],
        clock=clock,
    )


def run_structured_request(
    *,
    request: Dict[str, Any],
    confirmation: Dict[str, Any],
    event_log_path: Path,
    adapter_registry: Mapping[str, AwardProviderAdapter] = DEFAULT_ADAPTER_REGISTRY,
    current_date: date | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Dict[str, Any]:
    request_id = str(request.get("request_id", "")).strip()
    original_text = str(request.get("original_text", "")).strip()
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
) -> List[Dict[str, Any]]:
    if "program" in request:
        return [
            run_structured_request(
                request=request,
                confirmation=confirmation,
                event_log_path=event_log_path,
                adapter_registry=adapter_registry,
                current_date=current_date,
                clock=clock,
            )
        ]

    request_id = str(request.get("request_id", "")).strip()
    original_text = str(request.get("original_text", "")).strip()
    if parser is None:
        raise ValueError("parser is required for natural-language requests")
    try:
        parse_result = parser.parse(
            request_id=request_id,
            original_text=original_text,
            current_date=current_date or date.today(),
            timezone_name=timezone_name,
        )
    except ParserFailure as exc:
        return [
            emit_event(
                event_log_path=event_log_path,
                request_id=request_id,
                original_text=original_text,
                atomic_task_id=None,
                normalized_criteria=None,
                status="PARSER_FAILED",
                detail=str(exc),
                clock=clock,
                extra_fields={"diagnostic_metadata": exc.diagnostics},
            )
        ]
    except Exception as exc:
        return [
            emit_event(
                event_log_path=event_log_path,
                request_id=request_id,
                original_text=original_text,
                atomic_task_id=None,
                normalized_criteria=None,
                status="PARSER_FAILED",
                detail=str(exc),
                clock=clock,
                extra_fields={"diagnostic_metadata": {"parser_exception": type(exc).__name__}},
            )
        ]
    if parse_result.unsupported_reason is not None:
        return [
            emit_event(
                event_log_path=event_log_path,
                request_id=request_id,
                original_text=original_text,
                atomic_task_id=None,
                normalized_criteria=None,
                status="UNSUPPORTED_REQUEST",
                detail=parse_result.unsupported_reason,
                clock=clock,
                extra_fields={"diagnostic_metadata": parse_result.diagnostics or {}},
            )
        ]
    if parse_result.clarification is not None:
        return [
            emit_event(
                event_log_path=event_log_path,
                request_id=request_id,
                original_text=original_text,
                atomic_task_id=None,
                normalized_criteria=None,
                status="CLARIFICATION_REQUIRED",
                detail=parse_result.clarification,
                clock=clock,
                extra_fields={"diagnostic_metadata": parse_result.diagnostics or {}},
            )
        ]
    if not parse_result.requests:
        return [
            emit_event(
                event_log_path=event_log_path,
                request_id=request_id,
                original_text=original_text,
                atomic_task_id=None,
                normalized_criteria=None,
                status="PARSER_FAILED",
                detail="parser returned no executable request",
                clock=clock,
                extra_fields={"diagnostic_metadata": parse_result.diagnostics or {}},
            )
        ]

    structured_requests = [
        build_request_from_parsed_natural_language(request_id, original_text, parsed_request)
        for parsed_request in parse_result.requests
    ]
    normalized_requests = [
        normalize_request(
            structured_request,
            current_date=current_date,
            adapter_registry=adapter_registry,
        )
        for structured_request in structured_requests
    ]
    request_hash = build_request_set_hash(request_id, original_text, normalized_requests)
    confirmation_error = validate_confirmation(confirmation, request_id, request_hash)
    if confirmation_error is not None:
        normalized_criteria_set = [asdict(criteria) for criteria in normalized_requests]
        return [
            emit_event(
                event_log_path=event_log_path,
                request_id=request_id,
                original_text=original_text,
                atomic_task_id=None,
                normalized_criteria=normalized_criteria_set[0] if len(normalized_criteria_set) == 1 else None,
                status="CONFIRMATION_REQUIRED",
                detail=confirmation_error,
                clock=clock,
                extra_fields={
                    "request_hash": request_hash,
                    "normalized_criteria_set": normalized_criteria_set,
                    "diagnostic_metadata": parse_result.diagnostics or {},
                },
            )
        ]

    return [
        execute_confirmed_request(
            request_id=request_id,
            original_text=original_text,
            criteria=criteria,
            event_log_path=event_log_path,
            adapter_registry=adapter_registry,
            clock=clock,
        )
        for criteria in normalized_requests
    ]


def normalize_request(
    request: Dict[str, Any],
    *,
    current_date: date | None = None,
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
        request.get("departure_date"), current_date=current_date
    )
    cabin = normalize_cabin(request.get("cabin"))
    adults = request.get("adults")
    if adults != 1:
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


def normalize_airport(value: Any, field_name: str) -> str:
    airport = str(value or "").strip().upper()
    if (
        re.fullmatch(r"[A-Z]{3}", airport, flags=re.ASCII) is None
        or airport not in SUPPORTED_AIRPORTS
    ):
        raise ValueError(f"{field_name} must be a supported three-letter IATA code")
    return airport


def normalize_departure_date(value: Any, *, current_date: date | None = None) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw, flags=re.ASCII) is None:
        raise ValueError("departure_date must be an exact YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("departure_date must be an exact YYYY-MM-DD date") from exc
    if parsed < (current_date or date.today()):
        raise ValueError("departure_date cannot be in the past")
    return parsed.isoformat()


def normalize_cabin(value: Any) -> str:
    cabin_key = str(value or "").strip().lower().replace(" ", "_")
    if cabin_key not in SUPPORTED_CABINS:
        raise ValueError("cabin must be Economy, Premium Economy, Business, or First")
    return SUPPORTED_CABINS[cabin_key]


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
        "original_text": original_text,
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
    criteria = event.get("normalized_criteria") or {}
    criteria_report = ""
    if criteria:
        criteria_report = (
            f" program={criteria['program']} origin={criteria['origin']}"
            f" destination={criteria['destination']} departure_date={criteria['departure_date']}"
            f" cabin={criteria['cabin']} adults={criteria['adults']}"
            f" trip_type={criteria['trip_type']} maximum_points={criteria['maximum_points']}"
        )
    task = event["atomic_task_id"] or "none"
    return (
        f"{event['status']} request={event['request_id']}"
        f" original_text={event['original_text']} task={task}"
        f"{criteria_report} at {event['timestamp']}"
    )


def build_default_request_parser(model: str | None = None) -> GoogleGenAIRequestParser:
    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError("google-genai is required for natural-language requests") from exc
    return GoogleGenAIRequestParser(
        client=genai.Client(),
        model=model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
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
    return parser.parse_args()


def load_json(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    request = load_json(args.request)
    events = run_request(
        request=request,
        confirmation=load_json(args.confirmation),
        event_log_path=Path(args.event_log),
        parser=build_default_request_parser() if "program" not in request else None,
        current_date=args.current_date,
        timezone_name=args.timezone,
    )
    return 0 if all(event["status"] == "MATCH_FOUND" for event in events) else 1


if __name__ == "__main__":
    raise SystemExit(main())
