from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

from browser_use import Agent, BrowserSession
from browser_use.llm import ChatGoogle
from dotenv import load_dotenv
from steel import Steel

from .controlled_page import ControlledPageServer
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
    return parser.parse_args()


def build_agent_llms(*, primary_model: str, fallback_model: str, api_key: str):
    return (
        ChatGoogle(model=primary_model, api_key=api_key, max_retries=2),
        ChatGoogle(model=fallback_model, api_key=api_key, max_retries=2),
    )


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
    agent = Agent(
        task=task,
        llm=primary_llm,
        fallback_llm=fallback_llm,
        browser_session=browser,
        output_model_schema=ControlledPageResult,
        use_vision=False,
        max_actions_per_step=2,
        max_failures=2,
    )
    history = await agent.run(max_steps=config.max_steps)
    try:
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
) -> ControlledPageResult:
    session = client.sessions.create(headless=True)
    endpoints = build_session_endpoints(session)
    try:
        cdp_url = discover_debugger_cdp_url(config.steel_base_url, os.environ.get("STEEL_API_KEY"))
        return await run_agent_task(task=task, config=config, cdp_url=cdp_url)
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

    client = Steel(base_url=args.steel_base_url)
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
            steel_base_url=args.steel_base_url,
            controlled_page_url=f"{public_origin}/",
            storage_state_path=Path(args.storage_state_path),
            google_api_key=google_api_key,
            gemini_model=args.gemini_model,
            fallback_gemini_model=args.fallback_gemini_model,
            max_steps=args.max_steps,
        )
        ensure_storage_state_parent(config.storage_state_path)

        initial_result = await run_in_fresh_session(
            client=client,
            config=config,
            task=(
                f"Open {config.controlled_page_url}. Enter the marker value '{args.marker}' "
                "into the Marker input, click 'Save marker to this browser profile', and return "
                "the page title, the visible persisted marker, whether it is persisted, and the current URL."
            ),
        )

        persisted_result = await run_in_fresh_session(
            client=client,
            config=config,
            task=(
                f"Open {config.controlled_page_url}. Do not change the page. Read the visible page title, "
                "the persisted marker value, whether a marker is persisted, and the current URL."
            ),
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
                "initial_result": summary.initial_result.model_dump(),
                "persisted_result": summary.persisted_result.model_dump(),
                "offsite_rejection": summary.offsite_rejection,
                "handoff": {
                    "session_id": summary.handoff.session_id,
                    "status": summary.handoff.status.value,
                    "details_written": True,
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
