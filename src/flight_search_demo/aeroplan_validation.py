"""Fail-closed entry point for the first controlled Aeroplan validation.

No production browser driver has been validated. This entry point deliberately
cannot launch a browser or model, and never substitutes fixture results.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
from pathlib import Path
import sqlite3
from typing import Any

from .aeroplan import OFFICIAL_SEARCH_ENTRY_URL
from .app import build_request_hash, emit_event, load_json, normalize_request, validate_confirmation


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
    *, request: dict[str, Any], confirmation: dict[str, Any],
    risk_acknowledged: bool, event_log_path: Path, allowance_db_path: Path,
    current_date: date | None = None,
) -> dict[str, Any]:
    request_id = str(request.get("request_id", "")).strip()
    original_text = str(request.get("original_text", "")).strip()
    criteria = None
    task_id = None
    request_hash = None
    status = "MANUAL_SEARCH_ONLY"
    detail = ("Controlled live validation is blocked: no validated persistent-profile Aeroplan "
              "browser driver is available. No live navigation, model call, or submission occurred.")
    try:
        criteria = normalize_request(request, current_date=current_date)
        if criteria.program != "aeroplan":
            raise ValueError("controlled validation supports Aeroplan only")
        request_hash = build_request_hash(request_id, original_text, criteria)
        task_id = f"aeroplan-{request_hash[:12]}"
        error = validate_confirmation(confirmation, request_id, request_hash)
        if error:
            status, detail = "CONFIRMATION_REQUIRED", error
        elif risk_acknowledged is not True:
            status = "RISK_ACKNOWLEDGEMENT_REQUIRED"
            detail = "Explicit acknowledgement of Aeroplan account and terms risk is required."
    except ValueError as exc:
        status, detail = "UNSUPPORTED_REQUEST", str(exc)
    allowance = AeroplanAllowance(allowance_db_path)
    return emit_event(
        event_log_path=event_log_path, request_id=request_id, original_text=original_text,
        atomic_task_id=task_id, normalized_criteria=asdict(criteria) if criteria else None,
        status=status, detail=detail, extra_fields={
            "request_hash": request_hash,
            "adapter_status": "UNVERIFIED",
            "continuous_live_execution_enabled": False,
            "live_search_submitted": False,
            "live_validation_performed": False,
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
    parser.add_argument("--acknowledge-account-and-terms-risk", action="store_true")
    args = parser.parse_args()
    validate_controlled_search(
        request=load_json(args.request), confirmation=load_json(args.confirmation),
        risk_acknowledged=args.acknowledge_account_and_terms_risk,
        event_log_path=args.event_log, allowance_db_path=args.allowance_db,
    )
    return 1  # Blocked validation must never signal live success.


if __name__ == "__main__":
    raise SystemExit(main())
