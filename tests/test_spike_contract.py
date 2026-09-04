from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.parse import urlparse
import unittest

from flight_search_demo.models import BrowserStackConfig, HandoffStatus
from flight_search_demo.live_run import build_agent_llms
from flight_search_demo.live_run import assert_expected_results, classify_allowlist_rejection
from flight_search_demo.spike import (
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
        from flight_search_demo.models import ControlledPageResult

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
