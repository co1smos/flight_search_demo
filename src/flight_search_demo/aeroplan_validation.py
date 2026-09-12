"""Fail-closed entry point for the first controlled Aeroplan validation."""
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

from .aeroplan import (
    APPROVED_AIR_CANADA_DOMAINS,
    REQUIRED_IDENTITY_DOMAINS,
    AeroplanSearchAdapter,
    BrowserAgentOutcome,
    OFFICIAL_SEARCH_ENTRY_URL,
    PageSnapshot,
    PolicyViolation,
    UnsupportedDeterministicLayout,
)
from .app import build_request_hash, emit_event, load_json, normalize_request, validate_confirmation
from .spike import discover_debugger_cdp_url


OPERATION_DEADLINE_SECONDS = 60.0


@contextmanager
def _controller_deadline(seconds: float):
    """Interrupt the complete synchronous driver call at the controller seam."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("controlled live execution requires the main thread deadline guard")
    if seconds <= 0:
        raise TimeoutError("controlled Aeroplan operation exceeded its 60-second deadline")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expire(_signum, _frame):
        raise TimeoutError("controlled Aeroplan operation exceeded its 60-second deadline")

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


class ControlledAeroplanDriver(Protocol):
    """Narrow live-driver seam; implementations own browser cleanup and deadline enforcement."""

    def execute(
        self, criteria: Any, *, deadline_seconds: int, reserve_submission: Any,
    ) -> dict[str, Any]: ...


class PersistentAeroplanBrowser:
    """Browser adapter over the repository's persistent loopback Steel session."""

    def __init__(self, *, cdp_url: str, deadline_seconds: float, reserve_submission: Any):
        self.cdp_url = cdp_url
        self.deadline_seconds = deadline_seconds
        self._deadline_at = time.monotonic() + deadline_seconds
        self.reserve_submission = reserve_submission
        self.navigation_performed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: BrowserSession | None = None

    def _run(self, awaitable):
        if self._loop is None:
            raise RuntimeError("controlled browser is not open")
        remaining = self._deadline_at - time.monotonic()
        return self._loop.run_until_complete(
            asyncio.wait_for(awaitable, timeout=max(remaining, 0.001))
        )

    def __enter__(self) -> "PersistentAeroplanBrowser":
        self._loop = asyncio.new_event_loop()
        allowed = sorted(APPROVED_AIR_CANADA_DOMAINS | REQUIRED_IDENTITY_DOMAINS)
        self._session = BrowserSession(
            cdp_url=self.cdp_url,
            is_local=False,
            keep_alive=True,
            allowed_domains=allowed,
        )
        self._run(self._session.start())
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
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
            raise RuntimeError("controlled browser is not open")
        page = self._run(self._session.get_current_page())
        if page is None:
            raise RuntimeError("persistent browser has no controllable page")
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
        html = self._decode(
            self._run(page.evaluate("() => document.documentElement.outerHTML"))
        )
        if not isinstance(url, str) or not isinstance(html, str):
            raise RuntimeError("controlled browser returned non-text page content")
        return PageSnapshot(url=url, html=html)

    def open(self, url: str) -> PageSnapshot:
        page = self._page()
        self._run(page.goto(url))
        self.navigation_performed = True
        self._run(asyncio.sleep(min(3.0, self.deadline_seconds)))
        return self._snapshot()

    def deterministic_search(self, criteria: Any) -> PageSnapshot:
        """Use only explicitly named search controls and submit at most once."""
        page = self._page()
        fields = {
            "origin": (
                'input[name*="origin" i]', 'input[id*="origin" i]',
                'input[aria-label*="from" i]', 'input[placeholder*="from" i]',
            ),
            "destination": (
                'input[name*="destination" i]', 'input[id*="destination" i]',
                'input[aria-label*="to" i]', 'input[placeholder*="to" i]',
            ),
            "departure_date": (
                'input[type="date"]', 'input[name*="depart" i]', 'input[id*="depart" i]',
            ),
        }
        values = {
            "origin": criteria.origin,
            "destination": criteria.destination,
            "departure_date": criteria.departure_date,
        }
        for field, selectors in fields.items():
            element = None
            for selector in selectors:
                matches = self._run(page.get_elements_by_css_selector(selector))
                if matches:
                    element = matches[0]
                    break
            if element is None:
                raise UnsupportedDeterministicLayout(
                    f"official award form has no deterministic {field} control"
                )
            self._run(element.fill(str(values[field])))

        selections = {
            "cabin": (
                ('select[name*="cabin" i]', 'select[id*="cabin" i]'),
                criteria.cabin,
            ),
            "adults": (
                ('select[name*="adult" i]', 'select[id*="adult" i]'),
                str(criteria.adults),
            ),
            "trip_type": (
                ('select[name*="trip" i]', 'select[id*="trip" i]'),
                criteria.trip_type,
            ),
        }
        for field, (selectors, value) in selections.items():
            element = None
            for selector in selectors:
                matches = self._run(page.get_elements_by_css_selector(selector))
                if matches:
                    element = matches[0]
                    break
            if element is None:
                raise UnsupportedDeterministicLayout(
                    f"official award form has no deterministic {field} control"
                )
            self._run(element.select_option(str(value)))

        reward_toggle = None
        for selector in (
            'input[type="checkbox"][name*="redeem" i]',
            'input[type="checkbox"][id*="redeem" i]',
            'input[type="checkbox"][aria-label*="points" i]',
        ):
            matches = self._run(page.get_elements_by_css_selector(selector))
            if matches:
                reward_toggle = matches[0]
                break
        if reward_toggle is None:
            raise UnsupportedDeterministicLayout(
                "official form has no deterministic Aeroplan-reward control"
            )
        self._run(reward_toggle.check())

        submit = None
        for selector in (
            'button[aria-label*="search" i]', 'button[id*="search" i]',
        ):
            matches = self._run(page.get_elements_by_css_selector(selector))
            if matches:
                submit = matches[0]
                break
        if submit is None:
            raise UnsupportedDeterministicLayout(
                "official award form has no deterministic search submission control"
            )
        if not self.reserve_submission():
            raise PolicyViolation("Aeroplan submitted-search allowance is exhausted")
        self._run(submit.click())
        self._run(asyncio.sleep(min(3.0, self.deadline_seconds)))
        return self._snapshot()

    def browser_agent_search(
        self, criteria: Any, *, max_steps: int, allowed_domains: Any,
        authorize_action: Any,
    ) -> BrowserAgentOutcome:
        del criteria, max_steps, allowed_domains, authorize_action
        return BrowserAgentOutcome(
            status="refused",
            page=None,
            steps=0,
            visited_urls=(),
            actions=(),
            detail=(
                "official award layout is not deterministically recognized; "
                "no unbounded or policy-bypassing browser fallback was attempted"
            ),
        )


class PersistentAeroplanDriver:
    """Run the existing search policy/validator against persistent Steel Chromium."""

    def __init__(self, steel_base_url: str = "http://127.0.0.1:3000"):
        self.steel_base_url = str(steel_base_url)

    def execute(
        self, criteria: Any, *, deadline_seconds: int, reserve_submission: Any,
    ) -> dict[str, Any]:
        browser: PersistentAeroplanBrowser | None = None
        try:
            cdp_url = discover_debugger_cdp_url(
                self.steel_base_url,
                os.environ.get("STEEL_API_KEY"),
                timeout=max(float(deadline_seconds), 0.001),
            )
            browser = PersistentAeroplanBrowser(
                cdp_url=cdp_url,
                deadline_seconds=float(deadline_seconds),
                reserve_submission=reserve_submission,
            )
            with browser:
                result = AeroplanSearchAdapter(browser=browser).execute(
                    criteria, "controlled-live-aeroplan"
                )
        except TimeoutError:
            raise
        except Exception as exc:
            return {
                "status": "MANUAL_SEARCH_ONLY",
                "detail": (
                    "Controlled live validation could not acquire or safely operate the "
                    f"configured persistent browser: {type(exc).__name__}."
                ),
                "blocking_reason": "CONTROLLED_BROWSER_UNAVAILABLE",
                "live_validation_performed": bool(
                    browser is not None and browser.navigation_performed
                ),
                "visible_results_validated": False,
                "profile_reusable": False,
            }
        status = str(result.get("status", "PARSER_FAILED"))
        availability_statuses = {
            "MATCH_FOUND", "NO_AWARD_AVAILABILITY", "ABOVE_POINTS_LIMIT",
        }
        return {
            **result,
            "blocking_reason": status,
            "live_validation_performed": browser.navigation_performed,
            "visible_results_validated": status in availability_statuses,
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
    operation_started = time.monotonic()
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
            remaining = OPERATION_DEADLINE_SECONDS - (time.monotonic() - operation_started)
            with _controller_deadline(remaining):
                result = live_driver.execute(
                    criteria,
                    deadline_seconds=OPERATION_DEADLINE_SECONDS,
                    reserve_submission=reserve_submission,
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
            "operation_deadline_seconds": OPERATION_DEADLINE_SECONDS,
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
        "--steel-base-url",
        default=os.environ.get("STEEL_BASE_URL", "http://127.0.0.1:3000"),
        help="Loopback Steel service holding the dedicated persistent Chromium profile.",
    )
    parser.add_argument("--acknowledge-account-and-terms-risk", action="store_true")
    args = parser.parse_args()
    event = validate_controlled_search(
        request=load_json(args.request), confirmation=load_json(args.confirmation),
        risk_acknowledged=args.acknowledge_account_and_terms_risk,
        event_log_path=args.event_log, allowance_db_path=args.allowance_db,
        live_driver=PersistentAeroplanDriver(args.steel_base_url),
    )
    return 0 if event["adapter_status"] == "VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
