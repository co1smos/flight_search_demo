from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
import urllib.request

from .models import BrowserStackConfig, ControlledPageResult, HandoffStatus, HumanTakeoverGate, SessionEndpoints
from .security import assert_private_url, normalize_loopback_url, redact_secrets


def build_cdp_url(websocket_url: str, steel_api_key: Optional[str]) -> str:
    normalized = normalize_loopback_url(websocket_url)
    if steel_api_key and "apiKey=" not in normalized:
        separator = "&" if "?" in normalized else "?"
        return f"{normalized}{separator}apiKey={steel_api_key}"
    return normalized


def debugger_metadata_url(steel_base_url: str) -> str:
    parsed = urlparse(assert_private_url(steel_base_url))
    hostname = parsed.hostname or "127.0.0.1"
    return f"http://{hostname}:9223/json/version"


def build_debugger_cdp_url(debugger_websocket_url: str, steel_api_key: Optional[str]) -> str:
    parsed = urlparse(assert_private_url(normalize_loopback_url(debugger_websocket_url)))
    hostname = parsed.hostname or "127.0.0.1"
    netloc = f"{hostname}:9223"
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth = f"{auth}:{parsed.password}"
        netloc = f"{auth}@{netloc}"
    rebuilt = parsed._replace(netloc=netloc)
    cdp_url = rebuilt.geturl()
    if steel_api_key and "apiKey=" not in cdp_url:
        separator = "&" if "?" in cdp_url else "?"
        return f"{cdp_url}{separator}apiKey={steel_api_key}"
    return cdp_url


def discover_debugger_cdp_url(steel_base_url: str, steel_api_key: Optional[str]) -> str:
    with urllib.request.urlopen(debugger_metadata_url(steel_base_url)) as response:
        payload = json.loads(response.read().decode("utf-8"))
    debugger_url = payload["webSocketDebuggerUrl"]
    return build_debugger_cdp_url(debugger_url, steel_api_key)


def build_session_endpoints(session) -> SessionEndpoints:
    return SessionEndpoints(
        session_id=session.id,
        websocket_url=normalize_loopback_url(session.websocket_url),
        session_viewer_url=assert_private_url(normalize_loopback_url(session.session_viewer_url)),
        debug_url=assert_private_url(normalize_loopback_url(session.debug_url)),
    )


def build_takeover_gate(endpoints: SessionEndpoints) -> HumanTakeoverGate:
    gate = HumanTakeoverGate(session_id=endpoints.session_id, session_viewer_url=endpoints.session_viewer_url)
    gate.begin()
    return gate


def redact_runtime_text(text: str, steel_api_key: Optional[str], google_api_key: Optional[str]) -> str:
    secrets = [steel_api_key or "", google_api_key or ""]
    return redact_secrets(text, secrets)


@dataclass
class RunSummary:
    initial_result: ControlledPageResult | None
    persisted_result: ControlledPageResult | None
    offsite_rejection: str | None
    handoff: HumanTakeoverGate | None
    gemini_policy: dict[str, Any] = field(default_factory=dict)
    status: str = "SUCCEEDED"
    gemini_outcome: dict[str, Any] | None = None


def ensure_storage_state_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


def secure_artifact_file(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o600)


def write_private_handoff_file(gate: HumanTakeoverGate, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = {
        "session_id": gate.session_id,
        "session_viewer_url": gate.session_viewer_url,
        "resume_token": gate.resume_token,
        "status": gate.status.value,
    }
    _atomic_write_private_json(path, payload)


def _atomic_write_private_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def complete_handoff_file(path: Path, token: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != HandoffStatus.WAITING.value:
        raise RuntimeError("human takeover is not waiting")
    if token != payload.get("resume_token"):
        raise ValueError("invalid resume token")
    payload["status"] = HandoffStatus.COMPLETE.value
    _atomic_write_private_json(path, payload)


def handoff_is_complete(path: Path) -> bool:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("status") == HandoffStatus.COMPLETE.value
