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
    GeminiCallPolicy,
    GeminiErrorClassification,
    GeminiPolicyConfig,
    GeminiPolicyError,
)
from .models import BrowserStackConfig, ControlledPageResult
from .spike import (
    RunSummary,
    build_session_endpoints,
    build_takeover_gate,
    discover_debugger_cdp_url,
    ensure_storage_state_parent,
    handoff_is_complete,
    secure_artifact_file,
    write_private_handoff_file,
)
from .security import assert_private_url


def parse_args() -> argparse.Namespace:
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
    parser.add_argument("--gemini-run-call-limit", type=int, default=3)
    parser.add_argument("--gemini-usage-db-path", default=".artifacts/gemini-usage.sqlite3")
    return parser.parse_args()


def build_agent_llms(*, primary_model: str, fallback_model: str, api_key: str):
    from google.genai import types

    http_options = types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=1),
    )
    return (
        # Application policy owns retries; browser-use's SDK retry loop is one attempt.
        ChatGoogle(model=primary_model, api_key=api_key, max_retries=1, http_options=http_options),
        ChatGoogle(model=fallback_model, api_key=api_key, max_retries=1, http_options=http_options),
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


def assert_expected_results(
    initial_result: ControlledPageResult,
    persisted_result: ControlledPageResult,
    *,
    marker: str,
    controlled_page_url: str,
) -> None:
    expected_origin = urlparse(controlled_page_url)._replace(path="", params="", query="", fragment="").geturl()
    for name, result in (("initial", initial_result), ("persisted", persisted_result)):
        if result.marker_value != marker or not result.marker_persisted:
            raise RuntimeError(f"{name} marker did not match the expected persisted marker")
        result_origin = urlparse(result.current_url)._replace(path="", params="", query="", fragment="").geturl()
        if result_origin != expected_origin:
            raise RuntimeError(f"{name} result left the controlled origin")


async def run_agent_task(
    *,
    task: str,
    config: BrowserStackConfig,
    cdp_url: str,
    policy: GeminiCallPolicy | None = None,
    diagnostic_sink: Callable[[dict[str, Any]], None] | None = None,
) -> ControlledPageResult:
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
    if policy is None:
        policy = GeminiCallPolicy(
            db_path=config.gemini_usage_db_path,
            config=GeminiPolicyConfig(
                run_call_limit=config.gemini_run_call_limit,
                daily_call_limits=config.gemini_daily_call_limits,
                timezone_name=config.gemini_timezone_name,
                operation_deadline_seconds=config.operation_deadline_seconds,
            ),
        )
    operation = policy.operation(
        f"browser-agent:{hashlib.sha256(task.encode('utf-8')).hexdigest()[:12]}",
        task_id=hashlib.sha256(task.encode("utf-8")).hexdigest()[:12],
        deadline_seconds=config.operation_deadline_seconds,
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
    try:
        try:
            history = await asyncio.wait_for(
                agent.run(max_steps=config.max_steps),
                timeout=config.operation_deadline_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise GeminiPolicyError(
                "browser agent operation deadline exhausted",
                classification=GeminiErrorClassification.TIMEOUT_CANCELLATION,
                diagnostics=await operation.diagnostics_async(deadline_seconds=0.01),
            ) from exc
        if policy_llm.last_policy_error is not None:
            raise policy_llm.last_policy_error
        if history.structured_output is None:
            raise RuntimeError("browser-use returned no structured output")
        return history.structured_output
    finally:
        secure_artifact_file(config.storage_state_path)
        secure_artifact_file(config.storage_state_path.with_suffix(config.storage_state_path.suffix + ".bak"))


async def run_offsite_attempt(
    *,
    config: BrowserStackConfig,
    cdp_url: str,
) -> str:
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
    policy: GeminiCallPolicy | None = None,
    diagnostic_sink: Callable[[dict[str, Any]], None] | None = None,
) -> ControlledPageResult:
    session = client.sessions.create(headless=True)
    endpoints = build_session_endpoints(session)
    try:
        cdp_url = discover_debugger_cdp_url(config.steel_base_url, os.environ.get("STEEL_API_KEY"))
        return await run_agent_task(
            task=task,
            config=config,
            cdp_url=cdp_url,
            policy=policy,
            diagnostic_sink=diagnostic_sink,
        )
    finally:
        client.sessions.release(endpoints.session_id)


async def run_offsite_in_fresh_session(
    *,
    client: Steel,
    config: BrowserStackConfig,
) -> str:
    session = client.sessions.create(headless=True)
    endpoints = build_session_endpoints(session)
    try:
        cdp_url = discover_debugger_cdp_url(config.steel_base_url, os.environ.get("STEEL_API_KEY"))
        return await run_offsite_attempt(config=config, cdp_url=cdp_url)
    finally:
        client.sessions.release(endpoints.session_id)


async def run_spike(args: argparse.Namespace) -> RunSummary:
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
        assert_expected_results(
            initial_result,
            persisted_result,
            marker=args.marker,
            controlled_page_url=config.controlled_page_url,
        )

        handoff_session = client.sessions.create(headless=True)
        handoff_endpoints = build_session_endpoints(handoff_session)
        handoff_browser = BrowserSession(
            cdp_url=discover_debugger_cdp_url(config.steel_base_url, os.environ.get("STEEL_API_KEY")),
            is_local=False,
            keep_alive=True,
            allowed_domains=config.resolved_allowed_domains(),
            storage_state=str(config.storage_state_path),
        )
        await navigate_handoff_session(handoff_browser, config.controlled_page_url)
        handoff = build_takeover_gate(handoff_endpoints)
        handoff_path = Path(args.handoff_file)
        write_private_handoff_file(handoff, handoff_path)
        try:
            deadline = time.monotonic() + args.handoff_timeout
            while not handoff_is_complete(handoff_path):
                if time.monotonic() >= deadline:
                    raise TimeoutError("human takeover was not explicitly completed before the deadline")
                await asyncio.sleep(1)
            handoff.complete(handoff.resume_token)
            handoff.assert_resumable()
        finally:
            await handoff_browser.stop()
            client.sessions.release(handoff_endpoints.session_id)

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
                "gemini_policy": summary.gemini_policy,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
