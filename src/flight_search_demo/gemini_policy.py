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
import threading
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


def validate_model_configuration(primary_model: str, fallback_model: str) -> None:
    if not primary_model or not fallback_model:
        raise ValueError("primary and fallback Gemini models are required")
    if primary_model == fallback_model:
        raise ValueError("primary and fallback Gemini models must be different")


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

    value = _redact_serialized_sensitive_mappings(value)

    value = re.sub(
        r"(?im)(?P<key_quote>['\"]?)"
        r"(?P<label>\b(?:proxy[-_ ]?authorization|authorization|set[-_ ]?cookie|cookies?)\b)"
        r"(?P=key_quote)(?P<separator>\s*(?::|=)\s*)"
        r"(?P<value_quote>['\"])[^\r\n]*?(?P=value_quote)",
        lambda match: (
            f"{match.group('key_quote')}{match.group('label')}"
            f"{match.group('key_quote')}{match.group('separator')}"
            f"{match.group('value_quote')}[REDACTED]{match.group('value_quote')}"
        ),
        value,
    )
    value = re.sub(
        r"(?im)(?P<label>\b(?:proxy[-_ ]?authorization|authorization)\b)"
        r"(?P<separator>\s*(?::|=)\s*|\s+)[^\r\n]*",
        lambda match: match.group("label") + match.group("separator") + "[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?im)(?P<label>\b(?:set[-_ ]?cookie|cookies?)\b)"
        r"(?P<separator>\s*(?::|=)\s*|\s+)[^\r\n]*",
        lambda match: match.group("label") + match.group("separator") + "[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?im)(?P<label>\bcredentials?\b(?:\s+(?:are|is))?)"
        r"(?P<separator>\s*(?::|=)\s*|\s+)[^\r\n]*",
        lambda match: match.group("label") + match.group("separator") + "[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?im)(?P<scheme>\b(?:basic|bearer|digest)\b)(?P<separator>\s+)[^\r\n]*",
        lambda match: match.group("scheme") + match.group("separator") + "[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)(?P<label>\b(?:api[ _-]?key|access[ _-]?key|client[ _-]?secret|"
        r"secret[ _-]?key|password|secret|refresh[ _-]?token|auth[ _-]?token|token|"
        r"session(?:[ _-]?(?:id|token|key|material|cookie))?)\b)"
        r"(?P<separator>\s+(?:is|are)\s+|\s*[:=]\s*|\s+)[^\s,;]+",
        lambda match: match.group("label") + match.group("separator") + "[REDACTED]",
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

    return re.sub(r"(?:https?|wss?)://[^\s'\"<>]+", redact_url, value)


_SENSITIVE_MAPPING_KEY_MARKERS = (
    "apikey", "authorization", "credential", "credentials", "cookie", "password",
    "secret", "session", "token",
)


def _is_sensitive_mapping_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return any(marker in normalized for marker in _SENSITIVE_MAPPING_KEY_MARKERS)


def _quoted_end(value: str, start: int) -> int:
    quote = value[start]
    index = start + 1
    while index < len(value):
        if value[index] == "\\":
            index += 2
            continue
        if value[index] == quote:
            return index
        index += 1
    return len(value) - 1


def _container_end(value: str, start: int) -> int:
    pairs = {"[": "]", "(": ")", "{": "}"}
    stack: list[str] = [pairs[value[start]]]
    index = start + 1
    while index < len(value) and stack:
        character = value[index]
        if character in {"'", '"'}:
            index = _quoted_end(value, index) + 1
            continue
        if character in pairs:
            stack.append(pairs[character])
        elif character == stack[-1]:
            stack.pop()
        index += 1
    return min(index, len(value))


def _serialized_value_end(value: str, start: int) -> int:
    if start >= len(value):
        return start
    if value[start] in {"'", '"'}:
        return min(_quoted_end(value, start) + 1, len(value))
    if value[start] in "[{(":
        return _container_end(value, start)
    index = start
    while index < len(value) and value[index] not in ",]}):":
        index += 1
    return index


def _redact_serialized_sensitive_mappings(value: str) -> str:
    """Redact complete values for sensitive quoted mapping keys.

    Provider diagnostics often contain ``repr`` or JSON-like mappings. A
    regular expression can stop at the first nested quote, so scan only the
    mapping boundary and replace a scalar or balanced container as one value.
    """

    pieces: list[str] = []
    index = 0
    while index < len(value):
        if value[index] not in {"'", '"'}:
            pieces.append(value[index])
            index += 1
            continue
        key_start = index
        key_end = _quoted_end(value, index)
        key = value[index + 1:key_end]
        separator = key_end + 1
        while separator < len(value) and value[separator].isspace():
            separator += 1
        if (
            separator < len(value)
            and value[separator] in ":="
            and _is_sensitive_mapping_key(key)
        ):
            pieces.append(value[key_start:key_end + 1])
            pieces.append(value[key_end + 1:separator + 1])
            value_start = separator + 1
            while value_start < len(value) and value[value_start].isspace():
                value_start += 1
            pieces.append(value[separator + 1:value_start])
            value_end = _serialized_value_end(value, value_start)
            if value_start < value_end:
                if value[value_start] in {"'", '"'}:
                    quote = value[value_start]
                    pieces.append(quote + "[REDACTED]" + quote)
                else:
                    pieces.append("[REDACTED]")
            index = value_end
            continue
        pieces.append(value[key_start:key_end + 1])
        index = key_end + 1
    return "".join(pieces)


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, Mapping):
        header_name = next(
            (
                str(item)
                for key, item in value.items()
                if str(key).lower().replace("-", "_") in {"name", "header", "key"}
                and isinstance(item, str)
            ),
            None,
        )
        normalized_header_name = (
            re.sub(r"[^a-z0-9]", "", header_name.lower())
            if header_name is not None
            else ""
        )
        sensitive_header = any(
            marker in normalized_header_name
            for marker in ("authorization", "cookie", "apikey", "credential")
        )
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", key_text.lower())
            sensitive_key = any(marker in normalized_key for marker in _SENSITIVE_MAPPING_KEY_MARKERS)
            header_value = sensitive_header and normalized_key in {"value", "val", "content"}
            redacted[key_text] = "[REDACTED]" if sensitive_key or header_value else redact_sensitive(item)
        return redacted
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


class _RunBudget:
    """A process-local, concurrency-safe allowance for one policy/run scope."""

    def __init__(self, limit: int) -> None:
        self._remaining = limit
        self._lock = threading.Lock()

    def reserve(self) -> int | None:
        with self._lock:
            if self._remaining <= 0:
                return None
            self._remaining -= 1
            return self._remaining

    def release(self) -> None:
        with self._lock:
            self._remaining += 1

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._remaining


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
        defer_initialization: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.config = config or GeminiPolicyConfig()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._async_sleep = async_sleep or asyncio.sleep
        self._run_budget = _RunBudget(self.config.run_call_limit)
        self._storage_lock = threading.Lock()
        self._initialized = False
        if not defer_initialization:
            self._initialize_storage()

    def _initialize_storage(self, *, timeout_seconds: float = 30.0) -> None:
        if self._initialized:
            return
        with self._storage_lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect(timeout_seconds=timeout_seconds) as connection:
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
            self._initialized = True

    def initialize(self, operation: "GeminiOperation | None" = None) -> None:
        """Initialize persistent counters, optionally under an operation deadline."""
        if self._initialized:
            return
        if operation is None:
            self._initialize_storage()
        else:
            try:
                operation.run_sync(
                    lambda: self._initialize_storage(
                        timeout_seconds=max(0.001, operation.remaining_seconds())
                    )
                )
            except (TimeoutError, sqlite3.OperationalError) as exc:
                if (
                    isinstance(exc, sqlite3.OperationalError)
                    and "locked" not in str(exc).lower()
                    and operation.remaining_seconds() > 0
                ):
                    raise
                raise GeminiCallFailure(
                    "Gemini operation deadline exhausted during policy setup",
                    classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                    diagnostics=operation.diagnostics(
                        terminal_classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                        terminal_error="operation deadline exhausted during policy setup",
                        include_daily_reads=False,
                    ),
                ) from exc

    def _connect(self, *, timeout_seconds: float = 30.0) -> sqlite3.Connection:
        bounded_timeout = max(0.001, timeout_seconds)
        connection = sqlite3.connect(self.db_path, timeout=bounded_timeout)
        busy_timeout_ms = max(1, int(bounded_timeout * 1000))
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
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
            "day_boundary": "midnight",
            "usage_day": self.usage_day(),
            "max_attempts": self.config.max_attempts,
            "retry_backoff_seconds": list(self.config.retry_backoff_seconds),
            "operation_deadline_seconds": self.config.operation_deadline_seconds,
        }

    def _reserve(
        self,
        operation: "GeminiOperation",
        model: str,
        deadline_at: float | None = None,
        sqlite_timeout_seconds: float | None = None,
    ) -> _Reservation:
        daily_limit = self.config.daily_call_limits.get(model)
        if daily_limit is None:
            operation._record_reservation_denial(
                model=model,
                decision="blocked_daily_budget",
                allowance="daily_call_limit",
                allowance_limit=None,
                remaining_allowance=0,
                classification=GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
            )
            raise GeminiBudgetError(
                f"no daily Gemini allowance is configured for model {model}",
                classification=GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
                diagnostics=operation.diagnostics(
                    terminal_classification=GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
                    terminal_error=f"no daily allowance configured for model {model}",
                ),
            )

        remaining = None if deadline_at is None else deadline_at - self._monotonic()
        if remaining is not None and remaining <= 0:
            raise GeminiCallFailure(
                "Gemini operation deadline exhausted before reservation",
                classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                diagnostics=operation.diagnostics(
                    terminal_classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                    terminal_error="operation deadline exhausted before reservation",
                    include_daily_reads=False,
                ),
            )

        if not self._initialized:
            self.initialize(operation)

        usage_day = self.usage_day()
        try:
            with self._connect(
                timeout_seconds=(
                    sqlite_timeout_seconds
                    if sqlite_timeout_seconds is not None
                    else (30.0 if remaining is None else remaining)
                )
            ) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if deadline_at is not None and self._monotonic() >= deadline_at:
                    connection.rollback()
                    raise GeminiCallFailure(
                        "Gemini operation deadline exhausted before reservation",
                        classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                        diagnostics=operation.diagnostics(
                            terminal_classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                            terminal_error="operation deadline exhausted before reservation",
                            include_daily_reads=False,
                        ),
                    )
                row = connection.execute(
                    "SELECT attempted_calls FROM gemini_daily_usage WHERE usage_day=? AND timezone_name=? AND model=?",
                    (usage_day, self.config.timezone_name, model),
                ).fetchone()
                daily_used = int(row[0]) if row else 0
                if daily_used >= daily_limit:
                    connection.rollback()
                    operation._record_reservation_denial(
                        model=model,
                        decision="blocked_daily_budget",
                        allowance="daily_call_limit",
                        allowance_limit=daily_limit,
                        remaining_allowance=daily_limit - daily_used,
                        classification=GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
                    )
                    raise GeminiBudgetError(
                        f"daily Gemini allowance exhausted for model {model}",
                        classification=GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
                        diagnostics=operation.diagnostics(
                            terminal_classification=GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION,
                            terminal_error=f"daily allowance exhausted for model {model}",
                        ),
                    )
                run_remaining = self._run_budget.reserve()
                if run_remaining is None:
                    connection.rollback()
                    operation._record_reservation_denial(
                        model=model,
                        decision="blocked_run_budget",
                        allowance="run_call_limit",
                        allowance_limit=self.config.run_call_limit,
                        remaining_allowance=self._run_budget.remaining,
                        classification=GeminiErrorClassification.RUN_BUDGET_EXHAUSTION,
                    )
                    raise GeminiBudgetError(
                        "per-run Gemini call allowance exhausted",
                        classification=GeminiErrorClassification.RUN_BUDGET_EXHAUSTION,
                        diagnostics=operation.diagnostics(
                            terminal_classification=GeminiErrorClassification.RUN_BUDGET_EXHAUSTION,
                            terminal_error="per-run Gemini call allowance exhausted",
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
                try:
                    if deadline_at is not None and self._monotonic() >= deadline_at:
                        connection.rollback()
                        raise GeminiCallFailure(
                            "Gemini operation deadline exhausted before reservation",
                            classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                            diagnostics=operation.diagnostics(
                                terminal_classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                                terminal_error="operation deadline exhausted before reservation",
                                include_daily_reads=False,
                            ),
                        )
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    self._run_budget.release()
                    raise
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or (
                deadline_at is not None and self._monotonic() >= deadline_at
            ):
                raise GeminiCallFailure(
                    "Gemini operation deadline exhausted while reserving call budget",
                    classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                    diagnostics=operation.diagnostics(
                        terminal_classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                        terminal_error="operation deadline exhausted while reserving call budget",
                        include_daily_reads=False,
                    ),
                ) from exc
            raise
        operation.attempted_calls += 1
        operation._daily_remaining[model] = daily_limit - daily_used - 1
        return _Reservation(
            run_remaining=run_remaining,
            daily_remaining=daily_limit - daily_used - 1,
        )

    def daily_usage(self, model: str, *, timeout_seconds: float = 30.0) -> int:
        self._initialize_storage(timeout_seconds=timeout_seconds)
        with self._connect(timeout_seconds=timeout_seconds) as connection:
            row = connection.execute(
                "SELECT attempted_calls FROM gemini_daily_usage WHERE usage_day=? AND timezone_name=? AND model=?",
                (self.usage_day(), self.config.timezone_name, model),
            ).fetchone()
        return int(row[0]) if row else 0

    async def reserve_async(self, operation: "GeminiOperation", model: str) -> _Reservation:
        operation._ensure_time()
        # Poll SQLite with a short cooperative busy timeout so the event loop
        # remains cancellable without leaving an uncancellable worker behind.
        while True:
            remaining = operation.remaining_seconds()
            if remaining <= 0:
                raise GeminiCallFailure(
                    "Gemini operation deadline exhausted while reserving call budget",
                    classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                    diagnostics=operation.diagnostics(
                        terminal_classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                        terminal_error="operation deadline exhausted while reserving call budget",
                        include_daily_reads=False,
                    ),
                )
            try:
                return self._reserve(
                    operation,
                    model,
                    operation.deadline_at,
                    sqlite_timeout_seconds=min(0.01, remaining),
                )
            except GeminiCallFailure as exc:
                if (
                    exc.classification != GeminiErrorClassification.TIMEOUT_CANCELLATION
                    or operation.remaining_seconds() <= 0
                ):
                    raise
                await asyncio.sleep(min(0.005, operation.remaining_seconds()))

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
        self.reservation_denials: list[dict[str, Any]] = []
        self._daily_remaining: dict[str, int] = {}
        self.current_model: str | None = None
        self.current_purpose: str | None = None
        self._fallback_used = False
        self._fallback_model: str | None = None
        self._retry_number = 0

    def _record_reservation_denial(
        self,
        *,
        model: str,
        decision: str,
        allowance: str,
        allowance_limit: int | None,
        remaining_allowance: int,
        classification: GeminiErrorClassification,
    ) -> None:
        self.current_model = model
        denial = {
            "model": model,
            "purpose": self.current_purpose,
            "attempt_number": len(self.records) + 1,
            "decision": decision,
            "allowance": allowance,
            "allowance_limit": allowance_limit,
            "remaining_allowance": max(0, remaining_allowance),
            "classification": classification.value,
            "timestamp": _utc_timestamp(self.policy._now()),
            "provider_call_started": False,
        }
        self.reservation_denials.append(denial)

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

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_at - self.policy._monotonic())

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
        if self.remaining_seconds() <= delay:
            record.retry_decision = "deadline_exhausted"
            return False
        record.retry_decision = f"retry_after_{delay:g}s"
        try:
            self._run_sync_cooperatively(lambda: self.policy._sleep(delay))
        except TimeoutError:
            record.retry_decision = "deadline_exhausted"
            return False
        return True

    async def _prepare_async_retry(self, record: _CallRecord, retry_number: int) -> bool:
        delay = self._retry_delay(retry_number)
        if self.remaining_seconds() <= delay:
            record.retry_decision = "deadline_exhausted"
            return False
        record.retry_decision = f"retry_after_{delay:g}s"
        try:
            await asyncio.wait_for(self.policy._async_sleep(delay), timeout=self.remaining_seconds())
        except asyncio.TimeoutError:
            record.retry_decision = "deadline_exhausted"
            return False
        return True

    def _run_sync_cooperatively(self, callback: Callable[[], T]) -> T:
        """Run a sync callback without creating an uncancellable worker.

        External synchronous providers must receive their own native timeout
        from the caller. This seam executes callbacks directly and checks the
        shared deadline after they return; it never claims to cancel a callback
        that the provider cannot cancel.
        """
        if self.remaining_seconds() <= 0:
            raise TimeoutError("Gemini operation deadline exhausted")
        result = callback()
        if self.remaining_seconds() <= 0:
            raise TimeoutError("Gemini operation deadline exhausted")
        return result

    def ensure_time(self) -> None:
        """Require that the operation's original deadline has not elapsed."""
        self._ensure_time()

    def run_sync(self, callback: Callable[[], T]) -> T:
        """Run local setup directly under this operation's shared deadline."""
        return self._run_sync_cooperatively(callback)

    def diagnostics(
        self,
        *,
        terminal_classification: GeminiErrorClassification | None = None,
        terminal_error: str | None = None,
        include_daily_reads: bool = True,
        read_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        daily_remaining = {
            model: max(
                0,
                limit
                - self.policy.daily_usage(
                    model,
                    timeout_seconds=(
                        max(0.001, min(read_timeout_seconds, self.remaining_seconds()))
                        if read_timeout_seconds is not None
                        else 30.0
                    ),
                ),
            )
            for model, limit in self.policy.config.daily_call_limits.items()
        } if include_daily_reads else {
            model: self._daily_remaining.get(model, limit)
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

        reservation_denial = self.reservation_denials[-1] if self.reservation_denials else None
        latest_record = self.records[-1] if self.records else None
        diagnostic_model = (
            reservation_denial["model"]
            if reservation_denial is not None
            else (latest_record.model if latest_record is not None else self.current_model)
        )
        diagnostic_purpose = (
            reservation_denial["purpose"]
            if reservation_denial is not None
            else (latest_record.purpose if latest_record is not None else self.current_purpose)
        )
        diagnostic_attempt_number = (
            reservation_denial["attempt_number"]
            if reservation_denial is not None
            else (latest_record.attempt_number if latest_record is not None else None)
        )
        diagnostic_fallback_decision = (
            reservation_denial["decision"]
            if reservation_denial is not None
            else (latest_record.fallback_decision if latest_record is not None else "none")
        )

        payload: dict[str, Any] = {
            "operation_id": self.operation_id,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "purpose": diagnostic_purpose,
            "model": diagnostic_model,
            "attempt_number": diagnostic_attempt_number,
            "retry_decision": latest_record.retry_decision if latest_record else "none",
            "fallback_decision": diagnostic_fallback_decision,
            "timestamp": _utc_timestamp(self.policy._now()),
            "attempted_calls": self.attempted_calls,
            "successful_calls": sum(record.status == "successful" for record in self.records),
            "failed_calls": sum(record.status == "failed" for record in self.records),
            "retried_calls": sum(record.retry_decision.startswith("retry_after_") for record in self.records),
            "fallback_calls": sum(record.fallback_decision == "attempt" for record in self.records),
            "remaining_run_calls": self.policy._run_budget.remaining,
            "remaining_daily_calls": daily_remaining,
            "usage_by_model": usage_by_model,
            "counter_deltas": {
                "attempted": self.attempted_calls,
                "successful": sum(record.status == "successful" for record in self.records),
                "failed": sum(record.status == "failed" for record in self.records),
                "reservation_denied": len(self.reservation_denials),
            },
            "timezone_name": self.policy.config.timezone_name,
            "usage_day": self.policy.usage_day(),
            "call_records": [record.as_dict() for record in self.records],
            "reservation_denials": list(self.reservation_denials),
            "limits": self.policy.limits_summary(),
        }
        if reservation_denial is not None:
            payload["reservation_denial"] = reservation_denial
        if terminal_classification is not None:
            payload["terminal_classification"] = terminal_classification.value
        if terminal_error is not None:
            payload["terminal_error"] = redact_sensitive_text(terminal_error)
        return redact_sensitive(payload)

    async def diagnostics_async(self, *, deadline_seconds: float | None = None) -> dict[str, Any]:
        remaining = self.remaining_seconds()
        if deadline_seconds is not None:
            remaining = min(remaining, deadline_seconds)
        if remaining <= 0:
            return self.diagnostics(include_daily_reads=False)
        return self.diagnostics(read_timeout_seconds=remaining)

    def invoke(
        self,
        *,
        model: str,
        purpose: str,
        provider_call: Callable[[str], T],
        fallback_model: str | None = None,
    ) -> T:
        has_fallback = bool(fallback_model and fallback_model != model)
        fallback_used = (
            self._fallback_used
            and has_fallback
            and self._fallback_model == fallback_model
        )
        current_model = fallback_model if fallback_used else model
        self.current_model = model
        self.current_purpose = purpose
        attempts = 0
        primary_attempt_limit = max(
            1, self.policy.config.max_attempts - int(has_fallback)
        ) if has_fallback else self.policy.config.max_attempts
        primary_attempts = 0
        while attempts < self.policy.config.max_attempts:
            self._ensure_time()
            reservation = self.policy._reserve(
                self,
                current_model,
                deadline_at=self.deadline_at,
            )
            attempts += 1
            if current_model == model:
                primary_attempts += 1
            record = _CallRecord(
                model=current_model,
                purpose=purpose,
                attempt_number=len(self.records) + 1,
                started_at=_utc_timestamp(self.policy._now()),
                run_remaining=reservation.run_remaining,
                daily_remaining=reservation.daily_remaining,
            )
            if fallback_used:
                record.fallback_decision = "attempt"
            self.records.append(record)
            try:
                result = self._run_sync_cooperatively(lambda: provider_call(current_model))
                if inspect.isawaitable(result):
                    raise TypeError("sync Gemini provider callback returned an awaitable")
                record.status = "successful"
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                classification = self._finish_record(record, exc)
                if (
                    current_model == model
                    and classification in self.policy.config.retry_classifications
                    and primary_attempts < primary_attempt_limit
                    and attempts < self.policy.config.max_attempts
                ):
                    self._retry_number += 1
                    if self._prepare_retry(record, self._retry_number):
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
                    self._fallback_used = True
                    self._fallback_model = fallback_model
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
        has_fallback = bool(fallback_model and fallback_model != model)
        fallback_used = (
            self._fallback_used
            and has_fallback
            and self._fallback_model == fallback_model
        )
        current_model = fallback_model if fallback_used else model
        self.current_model = model
        self.current_purpose = purpose
        attempts = 0
        primary_attempt_limit = max(
            1, self.policy.config.max_attempts - int(has_fallback)
        ) if has_fallback else self.policy.config.max_attempts
        primary_attempts = 0
        while attempts < self.policy.config.max_attempts:
            self._ensure_time()
            reservation = await self.policy.reserve_async(self, current_model)
            attempts += 1
            if current_model == model:
                primary_attempts += 1
            record = _CallRecord(
                model=current_model,
                purpose=purpose,
                attempt_number=len(self.records) + 1,
                started_at=_utc_timestamp(self.policy._now()),
                run_remaining=reservation.run_remaining,
                daily_remaining=reservation.daily_remaining,
            )
            if fallback_used:
                record.fallback_decision = "attempt"
            self.records.append(record)
            try:
                result = await asyncio.wait_for(
                    provider_call(current_model), timeout=self.remaining_seconds()
                )
                record.status = "successful"
            except asyncio.CancelledError as exc:
                self._finish_record(record, exc)
                raise
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                classification = self._finish_record(record, exc)
                if (
                    current_model == model
                    and classification in self.policy.config.retry_classifications
                    and primary_attempts < primary_attempt_limit
                    and attempts < self.policy.config.max_attempts
                ):
                    self._retry_number += 1
                    if await self._prepare_async_retry(record, self._retry_number):
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
                    self._fallback_used = True
                    self._fallback_model = fallback_model
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
