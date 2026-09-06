from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

from browser_use import Agent, BrowserSession
from browser_use.llm import ChatGoogle
from dotenv import load_dotenv
from steel import Steel

from .controlled_page import ControlledPageServer
from .gemini_policy import (
    DEFAULT_DAILY_CALL_LIMITS,
    GeminiCallPolicy,
    GeminiErrorClassification,
    GeminiPolicyConfig,
    GeminiPolicyError,
    redact_sensitive_text,
    validate_model_configuration,
)
from .models import BrowserStackConfig, ControlledPageResult
from .spike import (
    RunSummary,
    BrowserTaskOutcome,
    build_session_endpoints,
    build_takeover_gate,
    discover_debugger_cdp_url,
    ensure_storage_state_parent,
    handoff_is_complete,
    secure_artifact_file,
    write_private_handoff_file,
)
from .security import assert_private_url


def _parse_retry_backoff(value: str) -> tuple[float, ...]:
    if not value.strip():
        return ()
    return tuple(float(item.strip()) for item in value.split(","))


def _parse_daily_call_limits(value: str | None) -> dict[str, int]:
    if not value:
        return {}
    limits: dict[str, int] = {}
    for item in value.split(","):
        model, separator, raw_limit = item.partition("=")
        if not separator or not model.strip():
            raise ValueError("daily Gemini limits must use MODEL=LIMIT entries")
        limits[model.strip()] = int(raw_limit.strip())
    return limits


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the controlled-page browser stack spike.")
    parser.add_argument("--steel-base-url", default=os.environ.get("STEEL_BASE_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--controlled-page-public-origin", default=os.environ.get("CONTROLLED_PAGE_PUBLIC_ORIGIN"))
    parser.add_argument("--controlled-page-port", type=int, default=8765)
    parser.add_argument(
        "--start-local-controlled-page-server",
        action="store_true",
        help="Serve the controlled page from this process instead of a sidecar container.",
    )
    parser.add_argument("--storage-state-path", default=".artifacts/controlled-page/storage-state.json")
    parser.add_argument("--handoff-file", default=".artifacts/controlled-page/handoff.json")
    parser.add_argument("--marker", default="marker-2026-09-03")
    parser.add_argument("--gemini-model", default=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
    parser.add_argument(
        "--fallback-gemini-model",
        default=os.environ.get("FALLBACK_GEMINI_MODEL", "gemini-3.6-flash"),
    )
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--handoff-timeout", type=int, default=300)
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
        type=_parse_retry_backoff,
        default=_parse_retry_backoff(os.environ.get("GEMINI_RETRY_BACKOFF_SECONDS", "0.25")),
    )
    parser.add_argument(
        "--gemini-operation-deadline-seconds",
        type=float,
        default=float(os.environ.get("GEMINI_OPERATION_DEADLINE_SECONDS", "60")),
    )
    args = parser.parse_args()
    raw_limits = args.gemini_daily_call_limits
    if isinstance(raw_limits, list):
        args.gemini_daily_call_limits = _parse_daily_call_limits(",".join(raw_limits))
    else:
        args.gemini_daily_call_limits = _parse_daily_call_limits(
            os.environ.get("GEMINI_DAILY_CALL_LIMITS")
        )
    return args


def build_agent_llms(*, primary_model: str, fallback_model: str, api_key: str):
    validate_model_configuration(primary_model, fallback_model)
    from google.genai import types

    http_options = types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=1),
    )
    return (
        # Application policy owns retries; the Google SDK request retry budget is one attempt.
        ChatGoogle(model=primary_model, api_key=api_key, http_options=http_options),
        ChatGoogle(model=fallback_model, api_key=api_key, http_options=http_options),
    )


class PolicyBoundChatGoogle:
    """Route browser-use Gemini calls through one application operation."""

    def __init__(
        self,
        *,
        primary: Any,
        fallback: Any,
        operation: Any,
        diagnostic_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._operation = operation
        self._diagnostic_sink = diagnostic_sink
        self.model = primary.model
        self.provider = "google"
        self._verified_api_keys = True
        self.last_policy_error: GeminiPolicyError | None = None

    @property
    def name(self) -> str:
        return str(self.model)

    @property
    def model_name(self) -> str:
        return str(self.model)

    async def ainvoke(self, messages, output_format=None, **kwargs):
        async def provider_call(model: str):
            llm = self._primary if model == self._primary.model else self._fallback
            return await llm.ainvoke(messages, output_format, **kwargs)

        try:
            return await self._operation.invoke_async(
                model=self._primary.model,
                purpose="browser_agent",
                provider_call=provider_call,
                fallback_model=self._fallback.model,
            )
        except GeminiPolicyError as exc:
            self.last_policy_error = exc
            raise
        finally:
            if self._diagnostic_sink is not None:
                self._diagnostic_sink(await self._operation.diagnostics_async())


def classify_allowlist_rejection(exc: Exception) -> str:
    if isinstance(exc, ValueError) and "blocked by security policy" in str(exc):
        return type(exc).__name__
    raise exc


def classify_deterministic_browser_failure(exc: Exception) -> str:
    if isinstance(exc, ValueError) and "blocked by security policy" in str(exc):
        return "domain_allowlist_rejection"
    if any(
        marker in str(exc).lower()
        for marker in ("captcha", "challenge", "verification", "access denied", "bot block")
    ):
        return "security_condition"
    raise exc


def _browser_outcome(
    *,
    operation,
    status: str,
    classification: str,
    detail: str,
    diagnostic_sink: Callable[[dict[str, Any]], None] | None,
) -> BrowserTaskOutcome:
    provenance = operation.diagnostics(include_daily_reads=False)
    provenance["classification"] = classification
    outcome = BrowserTaskOutcome(
        status=status,
        classification=classification,
        detail=redact_sensitive_text(detail),
        provenance=provenance,
    )
    if diagnostic_sink is not None:
        diagnostic_sink(outcome.provenance)
    return outcome


def validate_expected_result(
    result: ControlledPageResult,
    *,
    marker: str,
    controlled_page_url: str,
) -> None:
    expected_origin = urlparse(controlled_page_url)._replace(
        path="", params="", query="", fragment=""
    ).geturl()
    if result.marker_value != marker or not result.marker_persisted:
        raise RuntimeError("result marker did not match the expected persisted marker")
    result_origin = urlparse(result.current_url)._replace(
        path="", params="", query="", fragment=""
    ).geturl()
    if result_origin != expected_origin:
        raise RuntimeError("result left the controlled origin")


def assert_expected_results(
    initial_result: ControlledPageResult,
    persisted_result: ControlledPageResult,
    *,
    marker: str,
    controlled_page_url: str,
) -> None:
    for name, result in (("initial", initial_result), ("persisted", persisted_result)):
        try:
            validate_expected_result(
                result,
                marker=marker,
                controlled_page_url=controlled_page_url,
            )
        except RuntimeError as exc:
            detail = str(exc)
            if "marker" in detail:
                raise RuntimeError(f"{name} marker did not match the expected persisted marker") from exc
            raise RuntimeError(f"{name} result left the controlled origin") from exc


async def run_agent_task(
    *,
    task: str,
    config: BrowserStackConfig,
    cdp_url: str,
    policy: GeminiCallPolicy,
    diagnostic_sink: Callable[[dict[str, Any]], None] | None = None,
    expected_marker: str | None = None,
    expected_controlled_page_url: str | None = None,
    operation: Any | None = None,
) -> ControlledPageResult | BrowserTaskOutcome:
    if policy is None:
        raise ValueError("browser execution requires a caller-owned Gemini policy")
    validate_model_configuration(config.gemini_model, config.fallback_gemini_model)
    if operation is None:
        operation = policy.operation(
            f"browser-agent:{hashlib.sha256(task.encode('utf-8')).hexdigest()[:12]}",
            task_id=hashlib.sha256(task.encode("utf-8")).hexdigest()[:12],
            deadline_seconds=config.operation_deadline_seconds,
        )
    operation.current_model = config.gemini_model
    operation.current_purpose = "browser_agent"
    try:
        assert_private_url(cdp_url)
    except ValueError as exc:
        return _browser_outcome(
            operation=operation,
            status="BROWSER_SECURITY_REJECTED",
            classification="private_cdp_endpoint_rejection",
            detail=str(exc),
            diagnostic_sink=diagnostic_sink,
        )
    try:
        def setup() -> tuple[BrowserSession, PolicyBoundChatGoogle, Agent]:
            browser = BrowserSession(
                cdp_url=cdp_url,
                is_local=False,
                keep_alive=False,
                allowed_domains=config.resolved_allowed_domains(),
                storage_state=str(config.storage_state_path),
            )
            primary_llm, fallback_llm = build_agent_llms(
                primary_model=config.gemini_model,
                fallback_model=config.fallback_gemini_model,
                api_key=config.google_api_key or "",
            )
            policy_llm = PolicyBoundChatGoogle(
                primary=primary_llm,
                fallback=fallback_llm,
                operation=operation,
                diagnostic_sink=diagnostic_sink,
            )
            agent = Agent(
                task=task,
                llm=policy_llm,
                browser_session=browser,
                output_model_schema=ControlledPageResult,
                use_vision=False,
                max_actions_per_step=2,
                max_failures=2,
                llm_timeout=max(1, int(config.operation_deadline_seconds)),
            )
            return browser, policy_llm, agent

        try:
            _, policy_llm, agent = operation.run_sync(setup)
            operation.ensure_time()
        except TimeoutError as exc:
            raise GeminiPolicyError(
                "browser agent operation deadline exhausted during setup",
                classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                diagnostics=operation.diagnostics(include_daily_reads=False),
            ) from exc
        try:
            history = await asyncio.wait_for(
                agent.run(max_steps=config.max_steps),
                timeout=operation.remaining_seconds(),
            )
        except asyncio.TimeoutError as exc:
            raise GeminiPolicyError(
                "browser agent operation deadline exhausted",
                classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                diagnostics=await operation.diagnostics_async(deadline_seconds=0.01),
            ) from exc
        except Exception as exc:
            try:
                classification = classify_deterministic_browser_failure(exc)
            except Exception:
                raise
            return _browser_outcome(
                operation=operation,
                status="BROWSER_SECURITY_REJECTED",
                classification=classification,
                detail=str(exc),
                diagnostic_sink=diagnostic_sink,
            )
        if policy_llm.last_policy_error is not None:
            raise policy_llm.last_policy_error
        result = getattr(history, "structured_output", None)
        if not isinstance(result, ControlledPageResult):
            return _browser_outcome(
                operation=operation,
                status="BROWSER_RESULT_INVALID",
                classification=GeminiErrorClassification.MALFORMED_OUTPUT.value,
                detail="browser-use returned missing or unusable structured output",
                diagnostic_sink=diagnostic_sink,
            )
        if expected_marker is not None or expected_controlled_page_url is not None:
            if expected_marker is None or expected_controlled_page_url is None:
                raise ValueError("expected marker and controlled URL must be provided together")
            try:
                validate_expected_result(
                    result,
                    marker=expected_marker,
                    controlled_page_url=expected_controlled_page_url,
                )
            except RuntimeError as exc:
                return _browser_outcome(
                    operation=operation,
                    status="BROWSER_RESULT_INVALID",
                    classification="final_result_validation",
                    detail=str(exc),
                    diagnostic_sink=diagnostic_sink,
                )
        return result
    finally:
        secure_artifact_file(config.storage_state_path)
        secure_artifact_file(config.storage_state_path.with_suffix(config.storage_state_path.suffix + ".bak"))


async def run_offsite_attempt(
    *,
    config: BrowserStackConfig,
    cdp_url: str,
) -> str:
    assert_private_url(cdp_url)
    browser = BrowserSession(
        cdp_url=cdp_url,
        is_local=False,
        keep_alive=False,
        allowed_domains=config.resolved_allowed_domains(),
        storage_state=str(config.storage_state_path),
    )
    await browser.start()
    try:
        await browser.navigate_to(config.controlled_page_url)
        try:
            await browser.navigate_to("https://example.com/")
        except Exception as exc:
            return classify_allowlist_rejection(exc)
        raise RuntimeError("expected the allowlist to reject offsite navigation")
    finally:
        await browser.stop()
        secure_artifact_file(config.storage_state_path)
        secure_artifact_file(config.storage_state_path.with_suffix(config.storage_state_path.suffix + ".bak"))


async def navigate_handoff_session(browser: BrowserSession, controlled_page_url: str) -> None:
    await browser.start()
    await browser.navigate_to(controlled_page_url)


async def run_in_fresh_session(
    *,
    client: Steel,
    config: BrowserStackConfig,
    task: str,
    policy: GeminiCallPolicy,
    diagnostic_sink: Callable[[dict[str, Any]], None] | None = None,
    expected_marker: str | None = None,
    expected_controlled_page_url: str | None = None,
) -> ControlledPageResult | BrowserTaskOutcome:
    if policy is None:
        raise ValueError("browser execution requires a caller-owned Gemini policy")
    task_hash = hashlib.sha256(task.encode("utf-8")).hexdigest()[:12]
    operation = policy.operation(
        f"browser-agent:{task_hash}",
        task_id=task_hash,
        deadline_seconds=config.operation_deadline_seconds,
    )
    operation.current_model = config.gemini_model
    operation.current_purpose = "browser_agent"
    session_id: str | None = None
    try:
        def setup() -> tuple[str, str]:
            nonlocal session_id
            session = client.sessions.create(
                headless=True,
                timeout=max(operation.remaining_seconds(), 0.001),
                max_retries=0,
            )
            session_id = str(session.id)
            endpoints = build_session_endpoints(session)
            cdp_url = discover_debugger_cdp_url(
                config.steel_base_url,
                os.environ.get("STEEL_API_KEY"),
                timeout=max(operation.remaining_seconds(), 0.001),
            )
            return session_id, cdp_url

        try:
            session_id, cdp_url = operation.run_sync(setup)
            operation.ensure_time()
        except TimeoutError as exc:
            raise GeminiPolicyError(
                "browser agent operation deadline exhausted during session setup",
                classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                diagnostics=operation.diagnostics(include_daily_reads=False),
            ) from exc
        return await run_agent_task(
            task=task,
            config=config,
            cdp_url=cdp_url,
            policy=policy,
            diagnostic_sink=diagnostic_sink,
            expected_marker=expected_marker,
            expected_controlled_page_url=expected_controlled_page_url,
            operation=operation,
        )
    finally:
        if session_id is not None:
            client.sessions.release(session_id)


async def run_offsite_in_fresh_session(
    *,
    client: Steel,
    config: BrowserStackConfig,
) -> str:
    session_id: str | None = None
    try:
        session = client.sessions.create(
            headless=True,
            timeout=max(config.operation_deadline_seconds, 0.001),
            max_retries=0,
        )
        session_id = str(session.id)
        endpoints = build_session_endpoints(session)
        cdp_url = discover_debugger_cdp_url(
            config.steel_base_url,
            os.environ.get("STEEL_API_KEY"),
            timeout=max(config.operation_deadline_seconds, 0.001),
        )
        return await run_offsite_attempt(config=config, cdp_url=cdp_url)
    finally:
        if session_id is not None:
            client.sessions.release(session_id)


async def run_spike(args: argparse.Namespace) -> RunSummary:
    validate_model_configuration(args.gemini_model, args.fallback_gemini_model)
    load_dotenv()
    google_api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not google_api_key:
        raise RuntimeError("GOOGLE_API_KEY or GEMINI_API_KEY is required")

    steel_base_url = assert_private_url(args.steel_base_url)
    client = Steel(base_url=steel_base_url)
    server = None
    try:
        public_origin = args.controlled_page_public_origin
        if args.start_local_controlled_page_server:
            server = ControlledPageServer(port=args.controlled_page_port)
            server.start()
            _, port = server.address
            public_origin = public_origin or f"http://127.0.0.1:{port}"
        if not public_origin:
            raise RuntimeError("controlled page origin is required when not starting the local server")
        config = BrowserStackConfig(
            steel_base_url=steel_base_url,
            controlled_page_url=f"{public_origin}/",
            storage_state_path=Path(args.storage_state_path),
            google_api_key=google_api_key,
            gemini_model=args.gemini_model,
            fallback_gemini_model=args.fallback_gemini_model,
            max_steps=args.max_steps,
            gemini_run_call_limit=getattr(args, "gemini_run_call_limit", 3),
            gemini_timezone_name=getattr(args, "gemini_timezone", "UTC"),
            gemini_daily_call_limits={
                **DEFAULT_DAILY_CALL_LIMITS,
                **getattr(args, "gemini_daily_call_limits", {}),
            },
            operation_deadline_seconds=getattr(args, "gemini_operation_deadline_seconds", 60.0),
            gemini_max_attempts=getattr(args, "gemini_max_attempts", 2),
            gemini_retry_backoff_seconds=getattr(args, "gemini_retry_backoff_seconds", (0.25,)),
            gemini_usage_db_path=Path(
                getattr(args, "gemini_usage_db_path", ".artifacts/gemini-usage.sqlite3")
            ),
        )
        ensure_storage_state_parent(config.storage_state_path)
        gemini_policy = GeminiCallPolicy(
            db_path=config.gemini_usage_db_path,
            config=GeminiPolicyConfig(
                run_call_limit=config.gemini_run_call_limit,
                daily_call_limits=config.gemini_daily_call_limits,
                timezone_name=config.gemini_timezone_name,
                max_attempts=config.gemini_max_attempts,
                retry_backoff_seconds=config.gemini_retry_backoff_seconds,
                operation_deadline_seconds=config.operation_deadline_seconds,
            ),
        )
        gemini_operations: list[dict[str, Any]] = []

        try:
            initial_result = await run_in_fresh_session(
                client=client,
                config=config,
                policy=gemini_policy,
                diagnostic_sink=gemini_operations.append,
                expected_marker=args.marker,
                expected_controlled_page_url=config.controlled_page_url,
                task=(
                    f"Open {config.controlled_page_url}. Enter the marker value '{args.marker}' "
                    "into the Marker input, click 'Save marker to this browser profile', and return "
                    "the page title, the visible persisted marker, whether it is persisted, and the current URL."
                ),
            )

            persisted_result = await run_in_fresh_session(
                client=client,
                config=config,
                policy=gemini_policy,
                diagnostic_sink=gemini_operations.append,
                expected_marker=args.marker,
                expected_controlled_page_url=config.controlled_page_url,
                task=(
                    f"Open {config.controlled_page_url}. Do not change the page. Read the visible page title, "
                    "the persisted marker value, whether a marker is persisted, and the current URL."
                ),
            )
        except GeminiPolicyError as exc:
            diagnostics = dict(exc.diagnostics)
            classification = exc.classification.value
            if exc.classification == GeminiErrorClassification.RUN_BUDGET_EXHAUSTION:
                status = "GEMINI_BUDGET_EXHAUSTED"
            elif exc.classification == GeminiErrorClassification.DAILY_QUOTA_EXHAUSTION:
                status = "GEMINI_QUOTA_EXHAUSTED"
            else:
                status = "GEMINI_FAILED"
            diagnostic_id = uuid4().hex
            outcome = {
                "status": status,
                "diagnostic_id": diagnostic_id,
                "classification": classification,
                "model": diagnostics.get("model"),
                "purpose": diagnostics.get("purpose"),
                "remaining_run_calls": diagnostics.get("remaining_run_calls"),
                "remaining_daily_calls": diagnostics.get("remaining_daily_calls", {}),
                "provenance": diagnostics,
            }
            return RunSummary(
                initial_result=None,
                persisted_result=None,
                offsite_rejection=None,
                handoff=None,
                gemini_policy={
                    "limits": gemini_policy.limits_summary(),
                    "operations": gemini_operations,
                },
                status=status,
                gemini_outcome=outcome,
            )
        if isinstance(initial_result, BrowserTaskOutcome):
            return RunSummary(
                initial_result=None,
                persisted_result=None,
                offsite_rejection=None,
                handoff=None,
                gemini_policy={
                    "limits": gemini_policy.limits_summary(),
                    "operations": gemini_operations,
                },
                status=initial_result.status,
                browser_outcome=initial_result.as_dict(),
            )
        if isinstance(persisted_result, BrowserTaskOutcome):
            return RunSummary(
                initial_result=initial_result,
                persisted_result=None,
                offsite_rejection=None,
                handoff=None,
                gemini_policy={
                    "limits": gemini_policy.limits_summary(),
                    "operations": gemini_operations,
                },
                status=persisted_result.status,
                browser_outcome=persisted_result.as_dict(),
            )
        try:
            assert_expected_results(
                initial_result,
                persisted_result,
                marker=args.marker,
                controlled_page_url=config.controlled_page_url,
            )
        except RuntimeError as exc:
            outcome = BrowserTaskOutcome(
                status="BROWSER_RESULT_INVALID",
                classification="final_result_validation",
                detail=redact_sensitive_text(str(exc)),
                provenance={
                    "attempted_calls": sum(
                        operation.get("attempted_calls", 0)
                        for operation in gemini_operations
                    ),
                    "remaining_run_calls": max(
                        0,
                        gemini_policy.config.run_call_limit
                        - sum(
                            operation.get("attempted_calls", 0)
                            for operation in gemini_operations
                        ),
                    ),
                    "classification": "final_result_validation",
                },
            )
            return RunSummary(
                initial_result=initial_result,
                persisted_result=persisted_result,
                offsite_rejection=None,
                handoff=None,
                gemini_policy={
                    "limits": gemini_policy.limits_summary(),
                    "operations": gemini_operations,
                },
                status=outcome.status,
                browser_outcome=outcome.as_dict(),
            )

        handoff_session = client.sessions.create(
            headless=True,
            timeout=max(float(args.handoff_timeout), 0.001),
            max_retries=0,
        )
        handoff_session_id = str(handoff_session.id)
        handoff_browser = None
        try:
            handoff_endpoints = build_session_endpoints(handoff_session)
            handoff_browser = BrowserSession(
                cdp_url=discover_debugger_cdp_url(
                    config.steel_base_url,
                    os.environ.get("STEEL_API_KEY"),
                    timeout=max(config.operation_deadline_seconds, 0.001),
                ),
                is_local=False,
                keep_alive=True,
                allowed_domains=config.resolved_allowed_domains(),
                storage_state=str(config.storage_state_path),
            )
            await navigate_handoff_session(handoff_browser, config.controlled_page_url)
            handoff = build_takeover_gate(handoff_endpoints)
            handoff_path = Path(args.handoff_file)
            write_private_handoff_file(handoff, handoff_path)
            deadline = time.monotonic() + args.handoff_timeout
            while not handoff_is_complete(handoff_path):
                if time.monotonic() >= deadline:
                    raise TimeoutError("human takeover was not explicitly completed before the deadline")
                await asyncio.sleep(1)
            handoff.complete(handoff.resume_token)
            handoff.assert_resumable()
        finally:
            if handoff_browser is not None:
                await handoff_browser.stop()
            client.sessions.release(handoff_session_id)

        offsite_rejection = await run_offsite_in_fresh_session(client=client, config=config)

        return RunSummary(
            initial_result=initial_result,
            persisted_result=persisted_result,
            offsite_rejection=offsite_rejection,
            handoff=handoff,
            gemini_policy={
                "limits": gemini_policy.limits_summary(),
                "operations": gemini_operations,
            },
            browser_outcome={
                "status": "BROWSER_SECURITY_CHECKED",
                "classification": "domain_allowlist_rejection",
                "detail": "offsite navigation was rejected by the browser allowlist",
                "provenance": {
                    "offsite_rejection": offsite_rejection,
                    "provider_calls": sum(
                        operation.get("attempted_calls", 0)
                        for operation in gemini_operations
                    ),
                },
            },
        )
    finally:
        if server is not None:
            server.stop()


def main() -> None:
    args = parse_args()
    summary = asyncio.run(run_spike(args))
    print(
        json.dumps(
            {
                "status": summary.status,
                "initial_result": (
                    summary.initial_result.model_dump()
                    if summary.initial_result is not None
                    else None
                ),
                "persisted_result": (
                    summary.persisted_result.model_dump()
                    if summary.persisted_result is not None
                    else None
                ),
                "offsite_rejection": summary.offsite_rejection,
                "handoff": (
                    {
                        "session_id": summary.handoff.session_id,
                        "status": summary.handoff.status.value,
                        "details_written": True,
                    }
                    if summary.handoff is not None
                    else None
                ),
                "gemini_outcome": summary.gemini_outcome,
                "browser_outcome": summary.browser_outcome,
                "gemini_policy": summary.gemini_policy,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
