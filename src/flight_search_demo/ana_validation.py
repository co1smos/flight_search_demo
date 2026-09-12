"""Fail-closed controlled live validation for ANA Mileage Club."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time
from typing import Any, Protocol

from browser_use import BrowserSession

from .ana import (
    APPROVED_ANA_DOMAINS,
    REQUIRED_ANA_IDENTITY_DOMAINS,
    AnaAgentOutcome,
    AnaSearchAdapter,
    OFFICIAL_ANA_SEARCH_ENTRY_URL,
)
from .aeroplan import PageSnapshot, UnsupportedDeterministicLayout
from .app import build_request_hash, emit_event, load_json, normalize_request, validate_confirmation
from .spike import discover_debugger_cdp_url


OPERATION_DEADLINE_SECONDS = 90.0
MAX_REPORTING_RESERVE_SECONDS = 1.0
DEFAULT_ANA_PROFILE = Path(".artifacts/ana-browser-profile/storage-state.json")


@contextmanager
def _controller_deadline(seconds: float):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("controlled ANA live execution requires the main thread")
    if seconds <= 0:
        raise TimeoutError("controlled ANA operation exceeded its 90-second deadline")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expire(_signum, _frame):
        raise TimeoutError("controlled ANA operation exceeded its 90-second deadline")

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds, min(0.01, max(seconds / 10, 0.001)))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


class ControlledAnaDriver(Protocol):
    def execute(
        self, criteria: Any, *, deadline_seconds: float, reserve_submission: Any,
    ) -> dict[str, Any]: ...


class PersistentAnaBrowser:
    """ANA-only persistent browser state attached to a private CDP endpoint."""

    def __init__(
        self, *, cdp_url: str, profile_path: Path, deadline_seconds: float,
        reserve_submission: Any,
    ) -> None:
        self.cdp_url = cdp_url
        self.profile_path = Path(profile_path)
        self.deadline_at = time.monotonic() + deadline_seconds
        self.reserve_submission = reserve_submission
        self.navigation_performed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: BrowserSession | None = None

    def _run(self, awaitable):
        if self._loop is None:
            raise RuntimeError("controlled ANA browser is not open")
        return self._loop.run_until_complete(asyncio.wait_for(
            awaitable, timeout=max(self.deadline_at - time.monotonic(), 0.001)
        ))

    def __enter__(self):
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.new_event_loop()
        self._session = BrowserSession(
            cdp_url=self.cdp_url,
            is_local=False,
            keep_alive=True,
            allowed_domains=sorted(APPROVED_ANA_DOMAINS | REQUIRED_ANA_IDENTITY_DOMAINS),
            storage_state=str(self.profile_path),
        )
        self._run(self._session.start())
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self._session is not None:
                self._run(self._session.stop())
        finally:
            if self._loop is not None:
                self._loop.close()
            self._loop = None
            self._session = None

    def _page(self):
        if self._session is None:
            raise RuntimeError("controlled ANA browser is not open")
        page = self._run(self._session.get_current_page())
        if page is None:
            raise RuntimeError("persistent ANA browser has no controllable page")
        return page

    @staticmethod
    def _decode(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def _snapshot(self) -> PageSnapshot:
        page = self._page()
        url = self._decode(self._run(page.evaluate("() => window.location.href")))
        html = self._decode(self._run(page.evaluate("() => document.documentElement.outerHTML")))
        if not isinstance(url, str) or not isinstance(html, str):
            raise RuntimeError("controlled ANA browser returned non-text content")
        return PageSnapshot(url, html)

    def open(self, url: str) -> PageSnapshot:
        self._run(self._page().goto(url))
        self.navigation_performed = True
        self._run(asyncio.sleep(min(3.0, max(self.deadline_at - time.monotonic(), 0.001))))
        return self._snapshot()

    def deterministic_search(self, criteria: Any) -> PageSnapshot:
        del criteria
        raise UnsupportedDeterministicLayout("ANA award flow requires bounded browser-agent interaction")

    def browser_agent_search(self, criteria: Any, **kwargs: Any) -> AnaAgentOutcome:
        del criteria, kwargs
        # Live model execution is deliberately disabled until a policy-owned Gemini
        # operation is configured for this persistent profile. Navigation above still
        # permits deterministic classification of login, session, and traffic blocks.
        return AnaAgentOutcome(
            status="refused", page=None, steps=0, failures=0,
            visited_urls=(), actions=(),
            detail="ANA live browser agent is not configured; manual search is required",
        )


class PersistentAnaDriver:
    def __init__(
        self, steel_base_url: str = "http://127.0.0.1:3001",
        profile_path: Path = DEFAULT_ANA_PROFILE,
    ) -> None:
        self.steel_base_url = str(steel_base_url)
        self.profile_path = Path(profile_path)

    def execute(
        self, criteria: Any, *, deadline_seconds: float, reserve_submission: Any,
    ) -> dict[str, Any]:
        browser: PersistentAnaBrowser | None = None
        deadline_at = time.monotonic() + deadline_seconds
        try:
            cdp_url = discover_debugger_cdp_url(
                self.steel_base_url, os.environ.get("STEEL_API_KEY"),
                timeout=max(deadline_at - time.monotonic(), 0.001),
            )
            browser = PersistentAnaBrowser(
                cdp_url=cdp_url, profile_path=self.profile_path,
                deadline_seconds=max(deadline_at - time.monotonic(), 0.001),
                reserve_submission=reserve_submission,
            )
            with browser:
                result = AnaSearchAdapter(browser=browser).execute(criteria, "controlled-live-ana")
        except TimeoutError:
            raise
        except Exception as exc:
            return {
                "status": "MANUAL_SEARCH_ONLY",
                "detail": f"Controlled ANA browser was unavailable or unsafe: {type(exc).__name__}.",
                "blocking_reason": "CONTROLLED_BROWSER_UNAVAILABLE",
                "live_validation_performed": bool(browser and browser.navigation_performed),
                "visible_results_validated": False,
                "profile_reusable": False,
            }
        status = str(result.get("status", "VALIDATION_REJECTED"))
        if status == "BROWSER_AGENT_FAILED":
            status = "MANUAL_SEARCH_ONLY"
            result = {**result, "status": status}
        availability = {"MATCH_FOUND", "NO_AWARD_AVAILABILITY", "ABOVE_POINTS_LIMIT"}
        return {
            **result,
            "blocking_reason": "BROWSER_AGENT_UNAVAILABLE" if status == "MANUAL_SEARCH_ONLY" else status,
            "live_validation_performed": browser.navigation_performed,
            "visible_results_validated": status in availability,
            "profile_reusable": True,
        }


class AnaAllowance:
    """Independent lifetime allowance of ten submitted ANA searches."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ana_allowance "
                "(id INTEGER PRIMARY KEY CHECK(id = 1), remaining INTEGER NOT NULL "
                "CHECK(remaining BETWEEN 0 AND 10))"
            )
            connection.execute("INSERT OR IGNORE INTO ana_allowance VALUES (1, 10)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=1)

    @property
    def remaining(self) -> int:
        with self._connect() as connection:
            return connection.execute("SELECT remaining FROM ana_allowance WHERE id = 1").fetchone()[0]

    def consume_submission(self) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE ana_allowance SET remaining = remaining - 1 WHERE id = 1 AND remaining > 0"
            )
            return cursor.rowcount == 1


def validate_controlled_ana_search(
    *, request: Any, confirmation: Any, risk_acknowledged: bool,
    event_log_path: Path, allowance_db_path: Path, current_date: date | None = None,
    live_driver: ControlledAnaDriver | None = None,
) -> dict[str, Any]:
    deadline_at = time.monotonic() + OPERATION_DEADLINE_SECONDS
    request_id = str(request.get("request_id", "")).strip() if isinstance(request, dict) else ""
    original_text = str(request.get("original_text", "")).strip() if isinstance(request, dict) else ""
    criteria = None
    request_hash = None
    task_id = None
    request_confirmed = False
    status = "MANUAL_SEARCH_ONLY"
    detail = "Controlled ANA live execution is unavailable; fixture behavior remains usable."
    try:
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        criteria = normalize_request(request, current_date=current_date)
        if criteria.program != "ana":
            raise ValueError("controlled validation supports ANA Mileage Club only")
        request_hash = build_request_hash(request_id, original_text, criteria)
        task_id = f"ana-{request_hash[:12]}"
        error = (
            "confirmation must be a JSON object" if not isinstance(confirmation, dict)
            else validate_confirmation(confirmation, request_id, request_hash)
        )
        request_confirmed = error is None
        if error:
            status, detail = "CONFIRMATION_REQUIRED", error
        elif risk_acknowledged is not True:
            status = "RISK_ACKNOWLEDGEMENT_REQUIRED"
            detail = "Explicit acknowledgement of ANA account and terms risk is required."
    except ValueError as exc:
        status, detail = "UNSUPPORTED_REQUEST", str(exc)

    allowance = AnaAllowance(allowance_db_path)
    live_search_submitted = False
    live_validation_performed = False
    visible_results_validated = False
    profile_reusable = False
    adapter_status = "UNVERIFIED"
    blocking_reason = "LIVE_DRIVER_UNAVAILABLE"
    if status == "MANUAL_SEARCH_ONLY" and live_driver is not None:
        def reserve_submission() -> bool:
            nonlocal live_search_submitted
            if live_search_submitted or not allowance.consume_submission():
                return False
            live_search_submitted = True
            return True

        try:
            remaining = deadline_at - time.monotonic() - min(
                MAX_REPORTING_RESERVE_SECONDS, OPERATION_DEADLINE_SECONDS / 5
            )
            with _controller_deadline(remaining):
                result = live_driver.execute(
                    criteria, deadline_seconds=remaining, reserve_submission=reserve_submission
                )
                if not isinstance(result, dict):
                    raise ValueError("ANA live driver returned unstructured output")
                status = str(result.get("status", "VALIDATION_REJECTED"))
                detail = str(result.get("detail", "ANA live driver returned no detail"))
                blocking_reason = str(result.get("blocking_reason", status))
                live_validation_performed = result.get("live_validation_performed") is True
                visible_results_validated = result.get("visible_results_validated") is True
                profile_reusable = result.get("profile_reusable") is True
                availability = {"MATCH_FOUND", "NO_AWARD_AVAILABILITY", "ABOVE_POINTS_LIMIT"}
                if status in availability and not (
                    live_search_submitted and live_validation_performed
                    and visible_results_validated and profile_reusable
                ):
                    status = "VALIDATION_REJECTED"
                    detail = "ANA availability lacked submission, visible validation, or reusable-profile evidence"
                    blocking_reason = status
                elif status in availability:
                    adapter_status = "VERIFIED"
        except (TimeoutError, ValueError) as exc:
            status = "SEARCH_TIMEOUT" if isinstance(exc, TimeoutError) else "VALIDATION_REJECTED"
            detail = str(exc)
            blocking_reason = status

    with _controller_deadline(deadline_at - time.monotonic()):
        return emit_event(
            event_log_path=event_log_path, request_id=request_id, original_text=original_text,
            atomic_task_id=task_id, normalized_criteria=asdict(criteria) if criteria else None,
            status=status, detail=detail, extra_fields={
                "request_hash": request_hash,
                "risk_acknowledged": risk_acknowledged is True,
                "request_confirmed": request_confirmed,
                "blocking_reason": blocking_reason if status == "MANUAL_SEARCH_ONLY" else status,
                "adapter_status": adapter_status,
                "continuous_live_execution_enabled": False,
                "live_search_submitted": live_search_submitted,
                "live_validation_performed": live_validation_performed,
                "visible_results_validated": visible_results_validated,
                "profile_reusable": profile_reusable,
                "profile_kind": "ana_dedicated_storage_state",
                "allowance_remaining": allowance.remaining,
                "operation_deadline_seconds": OPERATION_DEADLINE_SECONDS,
                "search_entry_url": OFFICIAL_ANA_SEARCH_ENTRY_URL,
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--event-log", required=True, type=Path)
    parser.add_argument("--allowance-db", type=Path, default=Path(".artifacts/ana-usage.sqlite3"))
    parser.add_argument("--profile", type=Path, default=DEFAULT_ANA_PROFILE)
    parser.add_argument(
        "--steel-base-url", default=os.environ.get("ANA_STEEL_BASE_URL", "http://127.0.0.1:3001")
    )
    parser.add_argument("--acknowledge-account-and-terms-risk", action="store_true")
    args = parser.parse_args()
    event = validate_controlled_ana_search(
        request=load_json(args.request), confirmation=load_json(args.confirmation),
        risk_acknowledged=args.acknowledge_account_and_terms_risk,
        event_log_path=args.event_log, allowance_db_path=args.allowance_db,
        live_driver=PersistentAnaDriver(args.steel_base_url, args.profile),
    )
    return 0 if event["adapter_status"] == "VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
