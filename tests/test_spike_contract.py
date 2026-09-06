from pathlib import Path
import argparse
import asyncio
import os
import tempfile
from types import SimpleNamespace
from urllib.parse import urlparse
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from flight_search_demo.models import BrowserStackConfig, HandoffStatus
from flight_search_demo.live_run import (
    assert_expected_results,
    build_agent_llms,
    classify_allowlist_rejection,
    run_agent_task,
    run_in_fresh_session,
    run_offsite_attempt,
    run_spike,
)
from flight_search_demo.models import ControlledPageResult
from flight_search_demo.gemini_policy import (
    GeminiCallPolicy,
    GeminiErrorClassification,
    GeminiPolicyError,
    GeminiPolicyConfig,
)
from flight_search_demo.spike import (
    BrowserTaskOutcome,
    build_cdp_url,
    build_debugger_cdp_url,
    build_session_endpoints,
    build_takeover_gate,
    debugger_metadata_url,
    redact_runtime_text,
    complete_handoff_file,
    secure_artifact_file,
    write_private_handoff_file,
)
from flight_search_demo.live_run import navigate_handoff_session


class SpikeContractTests(unittest.TestCase):
    def test_run_spike_rejects_public_steel_url_before_constructing_client(self) -> None:
        args = self.make_run_args(steel_base_url="https://steel.example.com")

        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}, clear=False), patch(
            "flight_search_demo.live_run.Steel"
        ) as steel:
            with self.assertRaisesRegex(ValueError, "private address or loopback"):
                asyncio.run(run_spike(args))

        steel.assert_not_called()

    def test_run_spike_orchestrates_fresh_sessions_handoff_and_allowlist(self) -> None:
        args = self.make_run_args()
        result = ControlledPageResult(
            page_title="Controlled Browser Stack Test",
            marker_value=args.marker,
            marker_persisted=True,
            current_url="http://127.0.0.1:8765/",
        )
        client = MagicMock()
        handoff_session = SimpleNamespace(
            id="handoff-session",
            websocket_url="ws://127.0.0.1:3000/",
            session_viewer_url="http://127.0.0.1:3000/ui",
            debug_url="http://127.0.0.1:3000/debug",
        )
        client.sessions.create.return_value = handoff_session
        browser = MagicMock()
        browser.stop = AsyncMock()

        with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}, clear=False), patch(
            "flight_search_demo.live_run.Steel", return_value=client
        ), patch(
            "flight_search_demo.live_run.run_in_fresh_session",
            new=AsyncMock(side_effect=[result, result]),
        ) as fresh_session, patch(
            "flight_search_demo.live_run.run_offsite_in_fresh_session",
            new=AsyncMock(return_value="ValueError"),
        ) as offsite, patch(
            "flight_search_demo.live_run.BrowserSession", return_value=browser
        ), patch(
            "flight_search_demo.live_run.discover_debugger_cdp_url",
            return_value="ws://127.0.0.1:9223/devtools/browser/test",
        ), patch(
            "flight_search_demo.live_run.navigate_handoff_session", new=AsyncMock()
        ), patch(
            "flight_search_demo.live_run.write_private_handoff_file"
        ), patch(
            "flight_search_demo.live_run.handoff_is_complete", return_value=True
        ):
            summary = asyncio.run(run_spike(args))

        self.assertEqual(fresh_session.await_count, 2)
        offsite.assert_awaited_once()
        self.assertEqual(summary.offsite_rejection, "ValueError")
        self.assertEqual(summary.handoff.status, HandoffStatus.COMPLETE)
        client.sessions.release.assert_called_once_with("handoff-session")

    @staticmethod
    def make_run_args(**overrides: object) -> argparse.Namespace:
        values = {
            "steel_base_url": "http://127.0.0.1:3000",
            "controlled_page_public_origin": "http://127.0.0.1:8765",
            "controlled_page_port": 8765,
            "start_local_controlled_page_server": False,
            "storage_state_path": ".artifacts/test/storage-state.json",
            "handoff_file": ".artifacts/test/handoff.json",
            "marker": "expected-marker",
            "gemini_model": "gemini-3.5-flash-lite",
            "fallback_gemini_model": "gemini-3.6-flash",
            "max_steps": 4,
            "handoff_timeout": 1,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_only_the_expected_security_policy_error_counts_as_allowlist_rejection(self) -> None:
        rejection = classify_allowlist_rejection(
            ValueError("Navigation to https://example.com/ blocked by security policy")
        )
        self.assertEqual(rejection, "ValueError")

        with self.assertRaises(ConnectionError):
            classify_allowlist_rejection(ConnectionError("CDP disconnected"))

        with self.assertRaises(ValueError):
            classify_allowlist_rejection(ValueError("unrelated browser failure"))

    def test_runner_rejects_schema_valid_but_incorrect_results(self) -> None:
        expected_url = "http://controlled.test/"
        good = ControlledPageResult(
            page_title="Controlled Browser Stack Test",
            marker_value="expected-marker",
            marker_persisted=True,
            current_url=expected_url,
        )
        assert_expected_results(good, good, marker="expected-marker", controlled_page_url=expected_url)

        wrong_marker = good.model_copy(update={"marker_value": "wrong"})
        with self.assertRaisesRegex(RuntimeError, "marker"):
            assert_expected_results(wrong_marker, good, marker="expected-marker", controlled_page_url=expected_url)

        wrong_origin = good.model_copy(update={"current_url": "https://example.com/"})
        with self.assertRaisesRegex(RuntimeError, "controlled origin"):
            assert_expected_results(good, wrong_origin, marker="expected-marker", controlled_page_url=expected_url)

    def test_browser_runner_requires_caller_owned_policy_before_client_construction(self) -> None:
        config = BrowserStackConfig(
            steel_base_url="http://127.0.0.1:3000",
            controlled_page_url="http://controlled.test/",
            storage_state_path=Path(".artifacts/owned-policy/state.json"),
            google_api_key="offline-test",
        )
        with patch("flight_search_demo.live_run.BrowserSession") as browser, patch(
            "flight_search_demo.live_run.build_agent_llms"
        ) as build_llms:
            with self.assertRaisesRegex(ValueError, "caller-owned Gemini policy"):
                asyncio.run(
                    run_agent_task(
                        task="deterministic check",
                        config=config,
                        cdp_url="ws://127.0.0.1:9223/devtools/browser/test",
                        policy=None,
                    )
                )
        browser.assert_not_called()
        build_llms.assert_not_called()

    def test_equal_primary_and_fallback_models_are_rejected_at_configuration_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "different"):
            BrowserStackConfig(
                steel_base_url="http://127.0.0.1:3000",
                controlled_page_url="http://controlled.test/",
                storage_state_path=Path(".artifacts/model-config/state.json"),
                gemini_model="same-model",
                fallback_gemini_model="same-model",
            )

        args = self.make_run_args(
            gemini_model="same-model",
            fallback_gemini_model="same-model",
        )
        with patch("flight_search_demo.live_run.Steel") as steel:
            with self.assertRaisesRegex(ValueError, "different"):
                asyncio.run(run_spike(args))
        steel.assert_not_called()

    def test_deterministic_browser_paths_return_structured_zero_call_outcomes(self) -> None:
        calls = []

        class RecordingLLM:
            model = "offline-primary"

            async def ainvoke(self, *args, **kwargs):
                calls.append((args, kwargs))
                raise AssertionError("deterministic browser paths must not call the model")

        class RecordingBrowser:
            async def navigate_to(self, url: str) -> None:
                if "example.com" in url:
                    raise ValueError("Navigation to https://example.com/ blocked by security policy")

        class RecordingAgent:
            def __init__(self, *, task, llm, browser_session, **kwargs):
                self.task = task
                self.llm = llm
                self.browser_session = browser_session

            async def run(self, max_steps):
                if self.task == "domain":
                    await self.browser_session.navigate_to("https://example.com/")
                if self.task == "security":
                    raise RuntimeError("CAPTCHA challenge requires human intervention")
                return SimpleNamespace(
                    structured_output=ControlledPageResult(
                        page_title="Controlled Browser Stack Test",
                        marker_value="wrong-marker" if self.task == "result" else "expected-marker",
                        marker_persisted=True,
                        current_url="http://controlled.test/",
                    )
                )

        config = BrowserStackConfig(
            steel_base_url="http://127.0.0.1:3000",
            controlled_page_url="http://controlled.test/",
            storage_state_path=Path(".artifacts/zero-budget/state.json"),
            google_api_key="offline-test",
        )

        def build_recording_llms(**kwargs):
            return RecordingLLM(), RecordingLLM()

        def run(task: str, cdp_url: str):
            with tempfile.TemporaryDirectory() as tmpdir:
                policy = GeminiCallPolicy(
                    db_path=Path(tmpdir) / "usage.sqlite3",
                    config=GeminiPolicyConfig(
                        run_call_limit=0,
                        daily_call_limits={"offline-primary": 1, "offline-fallback": 1},
                    )
                )
                with patch("flight_search_demo.live_run.BrowserSession", return_value=RecordingBrowser()), patch(
                    "flight_search_demo.live_run.build_agent_llms", side_effect=build_recording_llms
                ), patch("flight_search_demo.live_run.Agent", RecordingAgent):
                    return asyncio.run(
                        run_agent_task(
                            task=task,
                            config=config,
                            cdp_url=cdp_url,
                            policy=policy,
                            expected_marker="expected-marker",
                            expected_controlled_page_url=config.controlled_page_url,
                        )
                    )

        outcomes = [
            run("domain", "ws://127.0.0.1:9223/devtools/browser/test"),
            run("security", "ws://127.0.0.1:9223/devtools/browser/test"),
            run("result", "ws://127.0.0.1:9223/devtools/browser/test"),
            run("result", "ws://8.8.8.8:9223/devtools/browser/test"),
        ]

        self.assertTrue(all(isinstance(outcome, BrowserTaskOutcome) for outcome in outcomes))
        self.assertEqual(
            [outcome.classification for outcome in outcomes],
            [
                "domain_allowlist_rejection",
                "security_condition",
                "final_result_validation",
                "private_cdp_endpoint_rejection",
            ],
        )
        self.assertEqual(calls, [])
        for outcome in outcomes:
            self.assertEqual(outcome.provenance["attempted_calls"], 0)
            self.assertEqual(outcome.provenance["remaining_run_calls"], 0)
            self.assertTrue(outcome.status)

    def test_browser_setup_consumes_the_original_operation_deadline(self) -> None:
        ticks = [0.0]

        class DelayedBrowser:
            def __init__(self, **kwargs):
                ticks[0] = 0.06

        class FakeLLM:
            model = "primary"

        class RecordingAgent:
            def __init__(self, **kwargs):
                self.ran = False

            async def run(self, max_steps):
                self.ran = True
                return SimpleNamespace(
                    structured_output=ControlledPageResult(
                        page_title="Controlled Browser Stack Test",
                        marker_value="expected-marker",
                        marker_persisted=True,
                        current_url="http://controlled.test/",
                    )
                )

        config = BrowserStackConfig(
            steel_base_url="http://127.0.0.1:3000",
            controlled_page_url="http://controlled.test/",
            storage_state_path=Path(".artifacts/deadline/state.json"),
            google_api_key="offline-test",
            operation_deadline_seconds=0.05,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=1,
                    daily_call_limits={"primary": 1, "fallback": 1},
                    operation_deadline_seconds=0.05,
                ),
                monotonic=lambda: ticks[0],
            )
            with patch("flight_search_demo.live_run.BrowserSession", DelayedBrowser), patch(
                "flight_search_demo.live_run.build_agent_llms",
                return_value=(FakeLLM(), FakeLLM()),
            ), patch("flight_search_demo.live_run.Agent", RecordingAgent):
                with self.assertRaises(GeminiPolicyError) as failure:
                    asyncio.run(
                        run_agent_task(
                            task="deadline setup",
                            config=config,
                            cdp_url="ws://127.0.0.1:9223/devtools/browser/test",
                            policy=policy,
                        )
                    )

        self.assertEqual(failure.exception.classification, GeminiErrorClassification.TIMEOUT_CANCELLATION)
        self.assertEqual(failure.exception.diagnostics["attempted_calls"], 0)

    def test_fresh_session_client_setup_consumes_the_original_operation_deadline(self) -> None:
        ticks = [0.0]

        class Sessions:
            def create(self, **kwargs):
                ticks[0] = 0.06
                return SimpleNamespace(
                    id="session",
                    websocket_url="ws://127.0.0.1:3000/",
                    session_viewer_url="http://127.0.0.1:3000/ui",
                    debug_url="http://127.0.0.1:3000/debug",
                )

            def release(self, session_id):
                pass

        config = BrowserStackConfig(
            steel_base_url="http://127.0.0.1:3000",
            controlled_page_url="http://controlled.test/",
            storage_state_path=Path(".artifacts/fresh-deadline/state.json"),
            google_api_key="offline-test",
            operation_deadline_seconds=0.05,
        )
        client = SimpleNamespace(sessions=Sessions())
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=1,
                    daily_call_limits={"primary": 1, "fallback": 1},
                    operation_deadline_seconds=0.05,
                ),
                monotonic=lambda: ticks[0],
            )
            with patch(
                "flight_search_demo.live_run.discover_debugger_cdp_url",
                return_value="ws://127.0.0.1:9223/devtools/browser/test",
            ), patch(
                "flight_search_demo.live_run.run_agent_task", new=AsyncMock()
            ) as agent_task:
                with self.assertRaises(GeminiPolicyError) as failure:
                    asyncio.run(
                        run_in_fresh_session(
                            client=client,
                            config=config,
                            task="fresh session deadline",
                            policy=policy,
                        )
                    )

        self.assertEqual(failure.exception.classification, GeminiErrorClassification.TIMEOUT_CANCELLATION)
        agent_task.assert_not_awaited()

    def test_missing_browser_structured_output_is_a_malformed_non_success_outcome(self) -> None:
        class FakeLLM:
            model = "primary"

        class RecordingAgent:
            def __init__(self, **kwargs):
                pass

            async def run(self, max_steps):
                return SimpleNamespace(structured_output=None)

        config = BrowserStackConfig(
            steel_base_url="http://127.0.0.1:3000",
            controlled_page_url="http://controlled.test/",
            storage_state_path=Path(".artifacts/malformed/state.json"),
            google_api_key="offline-test",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = GeminiCallPolicy(
                db_path=Path(tmpdir) / "usage.sqlite3",
                config=GeminiPolicyConfig(
                    run_call_limit=1,
                    daily_call_limits={"primary": 1, "fallback": 1},
                ),
            )
            with patch("flight_search_demo.live_run.BrowserSession"), patch(
                "flight_search_demo.live_run.build_agent_llms",
                return_value=(FakeLLM(), FakeLLM()),
            ), patch("flight_search_demo.live_run.Agent", RecordingAgent):
                outcome = asyncio.run(
                    run_agent_task(
                        task="missing output",
                        config=config,
                        cdp_url="ws://127.0.0.1:9223/devtools/browser/test",
                        policy=policy,
                    )
                )

        self.assertIsInstance(outcome, BrowserTaskOutcome)
        self.assertEqual(outcome.classification, GeminiErrorClassification.MALFORMED_OUTPUT.value)
        self.assertNotEqual(outcome.status, "SUCCEEDED")
        self.assertIn("structured output", outcome.detail)

    def test_steel_control_and_debugger_surfaces_must_be_private(self) -> None:
        from flight_search_demo.spike import debugger_metadata_url

        self.assertEqual(
            debugger_metadata_url("http://127.0.0.1:3000"),
            "http://127.0.0.1:9223/json/version",
        )
        with self.assertRaisesRegex(ValueError, "private address or loopback"):
            debugger_metadata_url("https://steel.example.com")

    def test_agent_llms_use_an_independent_fallback_model(self) -> None:
        primary, fallback = build_agent_llms(
            primary_model="gemini-3.5-flash-lite",
            fallback_model="gemini-3.6-flash",
            api_key="secret",
        )

        self.assertEqual(primary.model, "gemini-3.5-flash-lite")
        self.assertEqual(fallback.model, "gemini-3.6-flash")

    def test_handoff_session_is_navigated_before_waiting_for_human(self) -> None:
        class RecordingBrowser:
            def __init__(self) -> None:
                self.started = False
                self.stopped = False
                self.urls = []

            async def start(self) -> None:
                self.started = True

            async def navigate_to(self, url: str) -> None:
                self.urls.append(url)

            async def stop(self) -> None:
                self.stopped = True

        browser = RecordingBrowser()
        import asyncio

        asyncio.run(navigate_handoff_session(browser, "http://controlled.test/"))

        self.assertTrue(browser.started)
        self.assertEqual(browser.urls, ["http://controlled.test/"])
        self.assertFalse(browser.stopped)

    def test_build_cdp_url_normalizes_loopback(self) -> None:
        cdp_url = build_cdp_url("ws://0.0.0.0:3000/", None)
        self.assertEqual(cdp_url, "ws://127.0.0.1:3000/")

    def test_build_cdp_url_appends_api_key(self) -> None:
        cdp_url = build_cdp_url("ws://127.0.0.1:3000/?sessionId=abc", "secret")
        self.assertEqual(cdp_url, "ws://127.0.0.1:3000/?sessionId=abc&apiKey=secret")

    def test_session_endpoints_require_private_hosts(self) -> None:
        session = SimpleNamespace(
            id="session-1",
            websocket_url="ws://0.0.0.0:3000/",
            session_viewer_url="http://0.0.0.0:3000/",
            debug_url="http://10.0.0.8:3000/v1/sessions/debug",
        )
        endpoints = build_session_endpoints(session)
        self.assertEqual(urlparse(endpoints.session_viewer_url).hostname, "127.0.0.1")
        self.assertEqual(urlparse(endpoints.debug_url).hostname, "10.0.0.8")

    def test_debugger_metadata_url_uses_9223(self) -> None:
        self.assertEqual(
            debugger_metadata_url("http://172.17.0.2:3000"),
            "http://172.17.0.2:9223/json/version",
        )

    def test_build_debugger_cdp_url_restores_9223(self) -> None:
        self.assertEqual(
            build_debugger_cdp_url("ws://172.17.0.2/devtools/browser/abc", None),
            "ws://172.17.0.2:9223/devtools/browser/abc",
        )

    def test_redaction_removes_secrets(self) -> None:
        text = "steel=abc google=xyz"
        self.assertEqual(redact_runtime_text(text, "abc", "xyz"), "steel=[REDACTED] google=[REDACTED]")

    def test_handoff_requires_explicit_completion(self) -> None:
        gate = build_takeover_gate(
            SimpleNamespace(
                session_id="session-1",
                session_viewer_url="http://127.0.0.1:3000/",
            )
        )
        self.assertEqual(gate.status, HandoffStatus.WAITING)
        with self.assertRaises(RuntimeError):
            gate.assert_resumable()
        with self.assertRaises(ValueError):
            gate.complete("wrong-token")
        gate.complete(gate.resume_token)
        gate.assert_resumable()
        self.assertEqual(gate.status, HandoffStatus.COMPLETE)

    def test_controlled_page_host_becomes_default_allowlist(self) -> None:
        config = BrowserStackConfig(
            steel_base_url="http://127.0.0.1:3000",
            controlled_page_url="http://172.17.0.1:8765/",
            storage_state_path=Path(".artifacts/state.json"),
        )
        self.assertEqual(config.resolved_allowed_domains(), ["172.17.0.1"])

    def test_handoff_details_are_written_to_private_file(self) -> None:
        gate = build_takeover_gate(
            SimpleNamespace(
                session_id="session-1",
                session_viewer_url="http://127.0.0.1:3000/",
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "handoff.json"
            write_private_handoff_file(gate, output)
            self.assertTrue(output.exists())
            self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")
            self.assertIn("resume_token", output.read_text(encoding="utf-8"))

    def test_handoff_is_completed_by_a_separate_explicit_operation(self) -> None:
        gate = build_takeover_gate(
            SimpleNamespace(
                session_id="session-1",
                session_viewer_url="http://127.0.0.1:3000/",
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "handoff.json"
            write_private_handoff_file(gate, output)

            with self.assertRaises(ValueError):
                complete_handoff_file(output, "wrong-token")

            complete_handoff_file(output, gate.resume_token)
            payload = output.read_text(encoding="utf-8")
            self.assertIn('"status": "complete"', payload)
            self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")

    def test_browser_state_artifacts_are_restricted_to_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "storage-state.json"
            output.write_text("{}", encoding="utf-8")
            output.chmod(0o644)

            secure_artifact_file(output)

            self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
