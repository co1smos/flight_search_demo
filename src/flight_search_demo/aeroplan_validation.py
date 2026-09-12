"""Fail-closed entry point for the first controlled Aeroplan validation."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
from pathlib import Path
import sqlite3
from typing import Any, Protocol

from .aeroplan import OFFICIAL_SEARCH_ENTRY_URL
from .app import build_request_hash, emit_event, load_json, normalize_request, validate_confirmation


class ControlledAeroplanDriver(Protocol):
    """Narrow live-driver seam; implementations own browser cleanup and deadline enforcement."""

    def execute(
        self, criteria: Any, *, deadline_seconds: int, reserve_submission: Any,
    ) -> dict[str, Any]: ...


class PersistentAeroplanDriver:
    """Fail-closed live-driver preflight for the dedicated Aeroplan profile.

    Historical issue-5 browser code is fixture-confined: it aborts every request
    outside its loopback fixture origin.  Reusing its policy and result validation
    does not make that fixture page a safe driver for the current Air Canada DOM.
    """

    def __init__(self, profile_path: Path):
        self.profile_path = Path(profile_path)

    def execute(
        self, criteria: Any, *, deadline_seconds: int, reserve_submission: Any,
    ) -> dict[str, Any]:
        del criteria, deadline_seconds, reserve_submission
        if not self.profile_path.is_dir():
            return {
                "status": "MANUAL_SEARCH_ONLY",
                "detail": (
                    "Controlled live validation is blocked: the dedicated persistent "
                    "Aeroplan browser profile is unavailable. No navigation, model call, "
                    "or submission occurred."
                ),
                "blocking_reason": "PERSISTENT_PROFILE_UNAVAILABLE",
                "live_validation_performed": False,
                "visible_results_validated": False,
                "profile_reusable": False,
            }
        return {
            "status": "MANUAL_SEARCH_ONLY",
            "detail": (
                "Controlled live validation is blocked: the historical controlled-browser "
                "implementation is fixture-confined and the official Air Canada layout has "
                "not been safely validated. No submission occurred."
            ),
            "blocking_reason": "OFFICIAL_LIVE_LAYOUT_UNVERIFIED",
            "live_validation_performed": False,
            "visible_results_validated": False,
            "profile_reusable": True,
        }


class AeroplanAllowance:
    """Lifetime submitted-search budget, independent of Gemini daily usage."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS aeroplan_allowance "
                               "(id INTEGER PRIMARY KEY CHECK(id = 1), remaining INTEGER NOT NULL "
                               "CHECK(remaining BETWEEN 0 AND 10))")
            connection.execute("INSERT OR IGNORE INTO aeroplan_allowance VALUES (1, 10)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=1)

    @property
    def remaining(self) -> int:
        with self._connect() as connection:
            return connection.execute("SELECT remaining FROM aeroplan_allowance WHERE id = 1").fetchone()[0]

    def consume_submission(self) -> bool:
        """Reserve immediately before dispatch; never refund an ambiguous timeout.

        Login/recovery must not call this method. A future live driver must bind
        this boundary to the actual submit action, with no retries or resubmits.
        """
        with self._connect() as connection:
            cursor = connection.execute("UPDATE aeroplan_allowance SET remaining = remaining - 1 "
                                        "WHERE id = 1 AND remaining > 0")
            return cursor.rowcount == 1


def validate_controlled_search(
    *, request: Any, confirmation: Any,
    risk_acknowledged: bool, event_log_path: Path, allowance_db_path: Path,
    current_date: date | None = None,
    live_driver: ControlledAeroplanDriver | None = None,
) -> dict[str, Any]:
    request_id = str(request.get("request_id", "")).strip() if isinstance(request, dict) else ""
    original_text = str(request.get("original_text", "")).strip() if isinstance(request, dict) else ""
    criteria = None
    task_id = None
    request_hash = None
    request_confirmed = False
    status = "MANUAL_SEARCH_ONLY"
    detail = ("Controlled live validation is blocked: no validated persistent-profile Aeroplan "
              "browser driver is available. No live navigation, model call, or submission occurred.")
    try:
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        criteria = normalize_request(request, current_date=current_date)
        if criteria.program != "aeroplan":
            raise ValueError("controlled validation supports Aeroplan only")
        request_hash = build_request_hash(request_id, original_text, criteria)
        task_id = f"aeroplan-{request_hash[:12]}"
        error = (
            "confirmation must be a JSON object"
            if not isinstance(confirmation, dict)
            else validate_confirmation(confirmation, request_id, request_hash)
        )
        request_confirmed = error is None
        if error:
            status, detail = "CONFIRMATION_REQUIRED", error
        elif risk_acknowledged is not True:
            status = "RISK_ACKNOWLEDGEMENT_REQUIRED"
            detail = "Explicit acknowledgement of Aeroplan account and terms risk is required."
    except ValueError as exc:
        status, detail = "UNSUPPORTED_REQUEST", str(exc)
    allowance = AeroplanAllowance(allowance_db_path)
    live_search_submitted = False
    live_validation_performed = False
    visible_results_validated = False
    profile_reusable = False
    adapter_status = "UNVERIFIED"
    continuous_live_execution_enabled = False
    blocking_reason = "LIVE_DRIVER_UNAVAILABLE"
    if status == "MANUAL_SEARCH_ONLY" and live_driver is not None:
        def reserve_submission() -> bool:
            nonlocal live_search_submitted
            if live_search_submitted:
                return False
            if not allowance.consume_submission():
                return False
            live_search_submitted = True
            return True

        try:
            result = live_driver.execute(
                criteria, deadline_seconds=60, reserve_submission=reserve_submission,
            )
            if not isinstance(result, dict):
                raise ValueError("live driver returned an unstructured result")
            status = str(result.get("status", "PARSER_FAILED"))
            detail = str(result.get("detail", "live driver returned no detail"))
            blocking_reason = str(result.get("blocking_reason", status))
            live_validation_performed = result.get("live_validation_performed") is True
            visible_results_validated = result.get("visible_results_validated") is True
            profile_reusable = result.get("profile_reusable") is True
            availability_statuses = {
                "MATCH_FOUND", "NO_AWARD_AVAILABILITY", "ABOVE_POINTS_LIMIT",
            }
            if status in availability_statuses and not (
                live_search_submitted
                and live_validation_performed
                and visible_results_validated
                and profile_reusable
            ):
                status = "PARSER_FAILED"
                detail = (
                    "live availability outcome lacked a submitted search, visible result "
                    "validation, or reusable-profile evidence"
                )
                blocking_reason = status
            elif status in availability_statuses:
                adapter_status = "VERIFIED"
        except (TimeoutError, ValueError) as exc:
            status = "SEARCH_TIMEOUT" if isinstance(exc, TimeoutError) else "PARSER_FAILED"
            detail = str(exc)
            blocking_reason = status
        continuous_live_execution_enabled = False
    return emit_event(
        event_log_path=event_log_path, request_id=request_id, original_text=original_text,
        atomic_task_id=task_id, normalized_criteria=asdict(criteria) if criteria else None,
        status=status, detail=detail, extra_fields={
            "request_hash": request_hash,
            "risk_acknowledged": risk_acknowledged is True,
            "request_confirmed": request_confirmed,
            "blocking_reason": (
                blocking_reason if status == "MANUAL_SEARCH_ONLY" else status
            ),
            "adapter_status": adapter_status,
            "continuous_live_execution_enabled": continuous_live_execution_enabled,
            "live_search_submitted": live_search_submitted,
            "live_validation_performed": live_validation_performed,
            "visible_results_validated": visible_results_validated,
            "profile_reusable": profile_reusable,
            "allowance_remaining": allowance.remaining,
            "operation_deadline_seconds": 60,
            "search_entry_url": OFFICIAL_SEARCH_ENTRY_URL,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--event-log", required=True, type=Path)
    parser.add_argument("--allowance-db", type=Path, default=Path(".artifacts/aeroplan-usage.sqlite3"))
    parser.add_argument(
        "--profile-dir", type=Path, default=Path(".artifacts/aeroplan-profile"),
        help="Dedicated persistent Aeroplan Chromium profile (never a personal browser profile).",
    )
    parser.add_argument("--acknowledge-account-and-terms-risk", action="store_true")
    args = parser.parse_args()
    validate_controlled_search(
        request=load_json(args.request), confirmation=load_json(args.confirmation),
        risk_acknowledged=args.acknowledge_account_and_terms_risk,
        event_log_path=args.event_log, allowance_db_path=args.allowance_db,
        live_driver=PersistentAeroplanDriver(args.profile_dir),
    )
    return 1  # Blocked validation must never signal live success.


if __name__ == "__main__":
    raise SystemExit(main())
