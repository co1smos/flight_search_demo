"""Application-owned limits and diagnostics for Gemini calls.

This module intentionally covers only the call seam used by the application. It
does not discover provider quotas or attempt to distribute traffic between keys
or models.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, TypeVar
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


T = TypeVar("T")


class GeminiErrorClassification(StrEnum):
    RPM_RATE_LIMIT = "rpm_rate_limit"
    DAILY_QUOTA_EXHAUSTION = "daily_quota_exhaustion"
    TRANSIENT_PROVIDER_ERROR = "transient_provider_error"
    AUTH_INVALID_ERROR = "auth_invalid_error"
    TIMEOUT_CANCELLATION = "timeout_cancellation"
    MALFORMED_OUTPUT = "malformed_output"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"
    RUN_BUDGET_EXHAUSTION = "run_budget_exhaustion"


class GeminiPolicyError(RuntimeError):
    """A non-success result from the application Gemini policy."""

    def __init__(
        self,
        message: str,
        *,
        classification: GeminiErrorClassification,
        diagnostics: Mapping[str, Any],
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.diagnostics = dict(diagnostics)


class GeminiBudgetError(GeminiPolicyError):
    pass


class GeminiCallFailure(GeminiPolicyError):
    pass


class MalformedModelOutputError(ValueError):
    """Raised by a provider callback when its model output is unusable."""


DEFAULT_DAILY_CALL_LIMITS = {
    # Conservative application defaults based on the observed free-tier slice.
    "gemini-2.5-flash": 5,
    "gemini-2.5-flash-lite": 15,
    "gemini-3.5-flash-lite": 15,
    "gemini-3.6-flash": 5,
}


@dataclass(frozen=True)
class GeminiPolicyConfig:
    run_call_limit: int = 3
    daily_call_limits: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_DAILY_CALL_LIMITS)
    )
    timezone_name: str = "UTC"
    max_attempts: int = 2
    retry_backoff_seconds: tuple[float, ...] = (0.25,)
    operation_deadline_seconds: float = 60.0
    retry_classifications: frozenset[GeminiErrorClassification] = frozenset(
        {GeminiErrorClassification.TRANSIENT_PROVIDER_ERROR}
    )
    fallback_classifications: frozenset[GeminiErrorClassification] = frozenset(
        {
            GeminiErrorClassification.RPM_RATE_LIMIT,
            GeminiErrorClassification.TRANSIENT_PROVIDER_ERROR,
            GeminiErrorClassification.MALFORMED_OUTPUT,
        }
    )

    def __post_init__(self) -> None:
        if self.run_call_limit < 0:
            raise ValueError("run_call_limit must be non-negative")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.operation_deadline_seconds <= 0:
            raise ValueError("operation_deadline_seconds must be positive")
        if not self.timezone_name:
            raise ValueError("timezone_name is required")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        for model, limit in self.daily_call_limits.items():
            if not model or not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                raise ValueError(f"daily call limit for {model!r} must be a non-negative integer")
        if any(delay < 0 for delay in self.retry_backoff_seconds):
            raise ValueError("retry backoff values must be non-negative")


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def redact_sensitive_text(value: str) -> str:
    """Remove credentials, session material, query values, and cookie values."""

    value = re.sub(
        r"(?i)\bauthorization\s*:\s*(?:bearer\s+)?[^\s,;]+",
        "authorization=[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)\b(?:api[_-]?key|authorization|cookie|password|secret|credential|token|session[_-]?id)\s*[:=]\s*[^\s,;]+",
        lambda match: match.group(0).split("=", 1)[0].split(":", 1)[0] + "=[REDACTED]",
        value,
    )
    value = re.sub(r"\bAIza[0-9A-Za-z_-]{20,}\b", "[REDACTED]", value)
    value = re.sub(r"\b(?:sk|key)-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)

    def redact_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parsed = urlsplit(raw)
            if parsed.scheme and parsed.netloc:
                host = parsed.hostname or "[REDACTED_HOST]"
                if parsed.port is not None:
                    host = f"{host}:{parsed.port}"
                return urlunsplit((parsed.scheme, host, "/[REDACTED]", "", ""))
        except ValueError:
            pass
        return "[REDACTED_URL]"

    return re.sub(r"https?://[^\s'\"<>]+", redact_url, value)


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, Mapping):
        return {str(key): redact_sensitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    return value


def _error_status_code(exc: BaseException) -> int | None:
    for candidate in (getattr(exc, "status_code", None), getattr(exc, "code", None)):
        if isinstance(candidate, int):
            return candidate
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def classify_gemini_error(exc: BaseException) -> GeminiErrorClassification:
    if isinstance(exc, (asyncio.CancelledError, TimeoutError, asyncio.TimeoutError)):
        return GeminiErrorClassification.TIMEOUT_CANCELLATION
    if isinstance(exc, MalformedModelOutputError):
        return GeminiErrorClassification.MALFORMED_OUTPUT

    message = str(exc).lower()
    status_code = _error_status_code(exc)
    if any(
        marker in message
        for marker in (
            "timed out", "timeout", "cancelled", "canceled",
        )
    ):
        return GeminiErrorClassification.TIMEOUT_CANCELLATION
    if any(
        marker in message
        for marker in (
            "failed to parse", "parse or validate response", "invalid json",
            "unusable structured data", "no response from model",
        )
    ):
        return GeminiErrorClassification.MALFORMED_OUTPUT
    daily_markers = (
        "per day", "daily quota", "daily limit", "rpd", "quota exceeded for day",
        "quota metric.*day",
    )
    if any(re.search(marker, message) for marker in daily_markers):
        return GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION
    if status_code == 429 or any(
        marker in message
        for marker in ("rate limit", "too many requests", "resource exhausted", "rpm", "429")
    ):
        return GeminiErrorClassification.RPM_RATE_LIMIT
    if status_code in (401, 403) or any(
        marker in message
        for marker in ("api key", "authentication", "unauthorized", "permission denied", "forbidden")
    ):
        return GeminiErrorClassification.AUTH_INVALID_ERROR
    if status_code in (408, 500, 502, 503, 504) or any(
        marker in message
        for marker in ("temporarily unavailable", "service unavailable", "internal server", "bad gateway", "connection reset")
    ):
        return GeminiErrorClassification.TRANSIENT_PROVIDER_ERROR
    if status_code == 400 or any(
        marker in message for marker in ("invalid request", "invalid argument", "malformed request")
    ):
        return GeminiErrorClassification.AUTH_INVALID_ERROR
    return GeminiErrorClassification.UNKNOWN_PROVIDER_ERROR


def _detail(exc: BaseException) -> str:
    return redact_sensitive_text(str(exc))[:500]


@dataclass
class _CallRecord:
    model: str
    purpose: str
    attempt_number: int
    started_at: str
    status: str = "attempted"
    classification: str | None = None
    error: str | None = None
    retry_decision: str = "none"
    fallback_decision: str = "none"
    run_remaining: int | None = None
    daily_remaining: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "model": self.model,
                "purpose": self.purpose,
                "attempt_number": self.attempt_number,
                "started_at": self.started_at,
                "status": self.status,
                "classification": self.classification,
                "error": self.error,
                "retry_decision": self.retry_decision,
                "fallback_decision": self.fallback_decision,
                "run_remaining": self.run_remaining,
                "daily_remaining": self.daily_remaining,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class _Reservation:
    run_remaining: int
    daily_remaining: int


class GeminiCallPolicy:
    """Reserve and account for every outbound application Gemini attempt."""

    def __init__(
        self,
        *,
        db_path: Path,
        config: GeminiPolicyConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        async_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.config = config or GeminiPolicyConfig()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._async_sleep = async_sleep or asyncio.sleep
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS gemini_daily_usage (
                    usage_day TEXT NOT NULL,
                    timezone_name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    attempted_calls INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (usage_day, timezone_name, model)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def usage_day(self) -> str:
        return self._now().astimezone(ZoneInfo(self.config.timezone_name)).date().isoformat()

    def limits_summary(self) -> dict[str, Any]:
        return {
            "run_call_limit": self.config.run_call_limit,
            "daily_call_limits": dict(self.config.daily_call_limits),
            "timezone_name": self.config.timezone_name,
            "usage_day": self.usage_day(),
            "max_attempts": self.config.max_attempts,
            "operation_deadline_seconds": self.config.operation_deadline_seconds,
        }

    def _reserve(self, operation: "GeminiOperation", model: str) -> _Reservation:
        if operation.attempted_calls >= self.config.run_call_limit:
            raise GeminiBudgetError(
                "per-run Gemini call allowance exhausted",
                classification=GeminiErrorClassification.RUN_BUDGET_EXHAUSTION,
                diagnostics=operation.diagnostics(
                    terminal_classification=GeminiErrorClassification.RUN_BUDGET_EXHAUSTION,
                    terminal_error="per-run Gemini call allowance exhausted",
                ),
            )
        daily_limit = self.config.daily_call_limits.get(model)
        if daily_limit is None:
            raise GeminiBudgetError(
                f"no daily Gemini allowance is configured for model {model}",
                classification=GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
                diagnostics=operation.diagnostics(
                    terminal_classification=GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
                    terminal_error=f"no daily allowance configured for model {model}",
                ),
            )

        usage_day = self.usage_day()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempted_calls FROM gemini_daily_usage WHERE usage_day=? AND timezone_name=? AND model=?",
                (usage_day, self.config.timezone_name, model),
            ).fetchone()
            daily_used = int(row[0]) if row else 0
            if daily_used >= daily_limit:
                connection.rollback()
                raise GeminiBudgetError(
                    f"daily Gemini allowance exhausted for model {model}",
                    classification=GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
                    diagnostics=operation.diagnostics(
                        terminal_classification=GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
                        terminal_error=f"daily allowance exhausted for model {model}",
                    ),
                )
            if row:
                connection.execute(
                    "UPDATE gemini_daily_usage SET attempted_calls=attempted_calls+1 WHERE usage_day=? AND timezone_name=? AND model=?",
                    (usage_day, self.config.timezone_name, model),
                )
            else:
                connection.execute(
                    "INSERT INTO gemini_daily_usage(usage_day, timezone_name, model, attempted_calls) VALUES (?, ?, ?, 1)",
                    (usage_day, self.config.timezone_name, model),
                )
            connection.commit()
        operation.attempted_calls += 1
        return _Reservation(
            run_remaining=self.config.run_call_limit - operation.attempted_calls,
            daily_remaining=daily_limit - daily_used - 1,
        )

    def daily_usage(self, model: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempted_calls FROM gemini_daily_usage WHERE usage_day=? AND timezone_name=? AND model=?",
                (self.usage_day(), self.config.timezone_name, model),
            ).fetchone()
        return int(row[0]) if row else 0

    def operation(
        self,
        operation_id: str,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
        deadline_seconds: float | None = None,
    ) -> "GeminiOperation":
        deadline = (
            self.config.operation_deadline_seconds
            if deadline_seconds is None
            else deadline_seconds
        )
        if deadline <= 0:
            raise ValueError("deadline_seconds must be positive")
        return GeminiOperation(
            policy=self,
            operation_id=operation_id,
            request_id=request_id,
            task_id=task_id,
            deadline_at=self._monotonic() + deadline,
        )


class GeminiOperation:
    def __init__(
        self,
        *,
        policy: GeminiCallPolicy,
        operation_id: str,
        request_id: str | None,
        task_id: str | None,
        deadline_at: float,
    ) -> None:
        self.policy = policy
        self.operation_id = operation_id
        self.request_id = request_id
        self.task_id = task_id
        self.deadline_at = deadline_at
        self.attempted_calls = 0
        self.records: list[_CallRecord] = []
        self.current_model: str | None = None
        self.current_purpose: str | None = None

    def _ensure_time(self) -> None:
        if self.policy._monotonic() >= self.deadline_at:
            raise GeminiCallFailure(
                "Gemini operation deadline exhausted",
                classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                diagnostics=self.diagnostics(
                    terminal_classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                    terminal_error="operation deadline exhausted before the next call",
                ),
            )

    def _finish_record(self, record: _CallRecord, exc: BaseException | None) -> GeminiErrorClassification | None:
        if exc is None:
            record.status = "successful"
            return None
        classification = classify_gemini_error(exc)
        record.status = "failed"
        record.classification = classification.value
        record.error = _detail(exc)
        return classification

    def _retry_delay(self, retry_number: int) -> float:
        delays = self.policy.config.retry_backoff_seconds
        if not delays:
            return 0.0
        return delays[min(retry_number - 1, len(delays) - 1)]

    def _prepare_retry(self, record: _CallRecord, retry_number: int) -> bool:
        delay = self._retry_delay(retry_number)
        if self.policy._monotonic() + delay >= self.deadline_at:
            record.retry_decision = "deadline_exhausted"
            return False
        record.retry_decision = f"retry_after_{delay:g}s"
        self.policy._sleep(delay)
        return True

    async def _prepare_async_retry(self, record: _CallRecord, retry_number: int) -> bool:
        delay = self._retry_delay(retry_number)
        if self.policy._monotonic() + delay >= self.deadline_at:
            record.retry_decision = "deadline_exhausted"
            return False
        record.retry_decision = f"retry_after_{delay:g}s"
        await self.policy._async_sleep(delay)
        return True

    def diagnostics(
        self,
        *,
        terminal_classification: GeminiErrorClassification | None = None,
        terminal_error: str | None = None,
    ) -> dict[str, Any]:
        daily_remaining = {
            model: max(0, limit - self.policy.daily_usage(model))
            for model, limit in self.policy.config.daily_call_limits.items()
        }
        usage_by_model: dict[str, dict[str, int]] = {}
        for record in self.records:
            model_usage = usage_by_model.setdefault(
                record.model,
                {"attempted": 0, "successful": 0, "failed": 0, "retried": 0, "fallback": 0},
            )
            model_usage["attempted"] += 1
            if record.status in {"successful", "failed"}:
                model_usage[record.status] += 1
            model_usage["retried"] += int(record.retry_decision.startswith("retry_after_"))
            model_usage["fallback"] += int(record.fallback_decision == "attempt")

        payload: dict[str, Any] = {
            "operation_id": self.operation_id,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "purpose": self.records[-1].purpose if self.records else self.current_purpose,
            "model": self.records[-1].model if self.records else self.current_model,
            "attempt_number": self.records[-1].attempt_number if self.records else None,
            "retry_decision": self.records[-1].retry_decision if self.records else "none",
            "fallback_decision": self.records[-1].fallback_decision if self.records else "none",
            "timestamp": _utc_timestamp(self.policy._now()),
            "attempted_calls": self.attempted_calls,
            "successful_calls": sum(record.status == "successful" for record in self.records),
            "failed_calls": sum(record.status == "failed" for record in self.records),
            "retried_calls": sum(record.retry_decision.startswith("retry_after_") for record in self.records),
            "fallback_calls": sum(record.fallback_decision == "attempt" for record in self.records),
            "remaining_run_calls": max(0, self.policy.config.run_call_limit - self.attempted_calls),
            "remaining_daily_calls": daily_remaining,
            "usage_by_model": usage_by_model,
            "counter_deltas": {
                "attempted": self.attempted_calls,
                "successful": sum(record.status == "successful" for record in self.records),
                "failed": sum(record.status == "failed" for record in self.records),
            },
            "timezone_name": self.policy.config.timezone_name,
            "usage_day": self.policy.usage_day(),
            "call_records": [record.as_dict() for record in self.records],
            "limits": self.policy.limits_summary(),
        }
        if terminal_classification is not None:
            payload["terminal_classification"] = terminal_classification.value
        if terminal_error is not None:
            payload["terminal_error"] = redact_sensitive_text(terminal_error)
        return redact_sensitive(payload)

    def invoke(
        self,
        *,
        model: str,
        purpose: str,
        provider_call: Callable[[str], T],
        fallback_model: str | None = None,
    ) -> T:
        current_model = model
        self.current_model = model
        self.current_purpose = purpose
        fallback_used = False
        attempts = 0
        retry_number = 0
        while attempts < self.policy.config.max_attempts:
            self._ensure_time()
            reservation = self.policy._reserve(self, current_model)
            attempts += 1
            record = _CallRecord(
                model=current_model,
                purpose=purpose,
                attempt_number=attempts,
                started_at=_utc_timestamp(self.policy._now()),
                run_remaining=reservation.run_remaining,
                daily_remaining=reservation.daily_remaining,
            )
            if fallback_used:
                record.fallback_decision = "attempt"
            self.records.append(record)
            try:
                result = provider_call(current_model)
                if inspect.isawaitable(result):
                    raise TypeError("sync Gemini provider callback returned an awaitable")
                record.status = "successful"
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                classification = self._finish_record(record, exc)
                if classification in self.policy.config.retry_classifications and attempts < self.policy.config.max_attempts:
                    retry_number += 1
                    if self._prepare_retry(record, retry_number):
                        continue
                if (
                    not fallback_used
                    and fallback_model
                    and fallback_model != model
                    and classification in self.policy.config.fallback_classifications
                    and attempts < self.policy.config.max_attempts
                ):
                    record.fallback_decision = "eligible"
                    current_model = fallback_model
                    self.current_model = current_model
                    fallback_used = True
                    continue
                raise GeminiCallFailure(
                    "Gemini provider call failed",
                    classification=classification or GeminiErrorClassification.UNKNOWN_PROVIDER_ERROR,
                    diagnostics=self.diagnostics(
                        terminal_classification=classification,
                        terminal_error=_detail(exc),
                    ),
                ) from exc
            return result
        raise GeminiCallFailure(
            "Gemini operation exhausted its configured attempts",
            classification=GeminiErrorClassification.UNKNOWN_PROVIDER_ERROR,
            diagnostics=self.diagnostics(
                terminal_classification=GeminiErrorClassification.UNKNOWN_PROVIDER_ERROR,
                terminal_error="configured attempts exhausted",
            ),
        )

    async def invoke_async(
        self,
        *,
        model: str,
        purpose: str,
        provider_call: Callable[[str], Awaitable[T]],
        fallback_model: str | None = None,
    ) -> T:
        current_model = model
        self.current_model = model
        self.current_purpose = purpose
        fallback_used = False
        attempts = 0
        retry_number = 0
        while attempts < self.policy.config.max_attempts:
            self._ensure_time()
            reservation = self.policy._reserve(self, current_model)
            attempts += 1
            record = _CallRecord(
                model=current_model,
                purpose=purpose,
                attempt_number=attempts,
                started_at=_utc_timestamp(self.policy._now()),
                run_remaining=reservation.run_remaining,
                daily_remaining=reservation.daily_remaining,
            )
            if fallback_used:
                record.fallback_decision = "attempt"
            self.records.append(record)
            try:
                result = await provider_call(current_model)
                record.status = "successful"
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                classification = self._finish_record(record, exc)
                if classification in self.policy.config.retry_classifications and attempts < self.policy.config.max_attempts:
                    retry_number += 1
                    if await self._prepare_async_retry(record, retry_number):
                        continue
                if (
                    not fallback_used
                    and fallback_model
                    and fallback_model != model
                    and classification in self.policy.config.fallback_classifications
                    and attempts < self.policy.config.max_attempts
                ):
                    record.fallback_decision = "eligible"
                    current_model = fallback_model
                    self.current_model = current_model
                    fallback_used = True
                    continue
                raise GeminiCallFailure(
                    "Gemini provider call failed",
                    classification=classification or GeminiErrorClassification.UNKNOWN_PROVIDER_ERROR,
                    diagnostics=self.diagnostics(
                        terminal_classification=classification,
                        terminal_error=_detail(exc),
                    ),
                ) from exc
            return result
        raise GeminiCallFailure(
            "Gemini operation exhausted its configured attempts",
            classification=GeminiErrorClassification.UNKNOWN_PROVIDER_ERROR,
            diagnostics=self.diagnostics(
                terminal_classification=GeminiErrorClassification.UNKNOWN_PROVIDER_ERROR,
                terminal_error="configured attempts exhausted",
            ),
        )
