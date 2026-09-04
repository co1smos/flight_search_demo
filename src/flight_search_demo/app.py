from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Tuple


CURRENT_DATE = date(2026, 9, 4)

SUPPORTED_CABINS = {
    "economy": "Economy",
    "premium_economy": "Premium Economy",
    "business": "Business",
    "first": "First",
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


def build_request_hash(request_id: str, criteria: NormalizedCriteria) -> str:
    payload = {"request_id": request_id, "criteria": asdict(criteria)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_structured_request(
    *,
    request: Dict[str, Any],
    confirmation: Dict[str, Any],
    event_log_path: Path,
) -> Dict[str, Any]:
    request_id = str(request.get("request_id", "")).strip()
    original_text = str(request.get("original_text", "")).strip()
    try:
        criteria = normalize_request(request)
    except ValueError as exc:
        return emit_event(
            event_log_path=event_log_path,
            request_id=request_id,
            original_text=original_text,
            atomic_task_id=None,
            normalized_criteria=None,
            status="UNSUPPORTED_REQUEST",
            detail=str(exc),
        )

    request_hash = build_request_hash(request_id, criteria)
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
        )

    atomic_task_id = "aeroplan-" + request_hash[:12]
    result = run_aeroplan_fixture(criteria, atomic_task_id)
    return emit_event(
        event_log_path=event_log_path,
        request_id=request_id,
        original_text=original_text,
        atomic_task_id=atomic_task_id,
        normalized_criteria=asdict(criteria),
        status=result["status"],
        detail=result["detail"],
    )


def normalize_request(request: Dict[str, Any]) -> NormalizedCriteria:
    request_id = str(request.get("request_id", "")).strip()
    if not request_id:
        raise ValueError("request_id is required")
    original_text = str(request.get("original_text", "")).strip()
    if not original_text:
        raise ValueError("original_text is required")

    program = str(request.get("program", "")).strip().lower()
    if program != "aeroplan":
        raise ValueError("program must be exactly 'aeroplan' for this slice")

    origin = normalize_airport(request.get("origin"), "origin")
    destination = normalize_airport(request.get("destination"), "destination")
    departure_date = normalize_departure_date(request.get("departure_date"))
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
    if len(airport) != 3 or not airport.isalpha():
        raise ValueError(f"{field_name} must be an exact three-letter IATA code")
    return airport


def normalize_departure_date(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("departure_date must be an exact YYYY-MM-DD date") from exc
    if parsed < CURRENT_DATE:
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


def run_aeroplan_fixture(criteria: NormalizedCriteria, atomic_task_id: str) -> Dict[str, str]:
    if (
        criteria.origin == "JFK"
        and criteria.destination == "CDG"
        and criteria.departure_date == "2026-11-05"
        and criteria.cabin == "Business"
        and criteria.maximum_points >= 60000
    ):
        return {
            "status": "MATCH_FOUND",
            "detail": f"fixture matched confirmed request via {atomic_task_id}",
        }
    return {
        "status": "NO_AWARD_AVAILABILITY",
        "detail": f"fixture found no qualifying itinerary via {atomic_task_id}",
    }


def emit_event(
    *,
    event_log_path: Path,
    request_id: str,
    original_text: str,
    atomic_task_id: str | None,
    normalized_criteria: Dict[str, Any] | None,
    status: str,
    detail: str,
) -> Dict[str, Any]:
    timestamp = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
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

    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    with event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")

    print(render_terminal_report(event))
    return event


def render_terminal_report(event: Dict[str, Any]) -> str:
    criteria = event.get("normalized_criteria") or {}
    route = ""
    if criteria:
        route = (
            f" {criteria['origin']}-{criteria['destination']} {criteria['departure_date']}"
            f" {criteria['cabin']} <= {criteria['maximum_points']}"
        )
    task = event["atomic_task_id"] or "none"
    return (
        f"{event['status']} request={event['request_id']} task={task}"
        f"{route} at {event['timestamp']}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a confirmed structured award request.")
    parser.add_argument("--request", required=True, help="Path to the structured JSON request file.")
    parser.add_argument("--confirmation", required=True, help="Path to the structured JSON confirmation file.")
    parser.add_argument("--event-log", required=True, help="Path to the append-only JSONL event log.")
    return parser.parse_args()


def load_json(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    run_structured_request(
        request=load_json(args.request),
        confirmation=load_json(args.confirmation),
        event_log_path=Path(args.event_log),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
