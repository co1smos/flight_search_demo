from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, Field


class ControlledPageResult(BaseModel):
    page_title: str = Field(description="The visible page title.")
    marker_value: str = Field(description="The marker value visible on the page.")
    marker_persisted: bool = Field(description="Whether the page reports a persisted marker.")
    current_url: str = Field(description="The final browser URL after the task completes.")


class HandoffStatus(str, Enum):
    READY = "ready"
    WAITING = "waiting_for_human"
    COMPLETE = "complete"


@dataclass
class BrowserStackConfig:
    steel_base_url: str
    controlled_page_url: str
    storage_state_path: Path
    allowed_domains: List[str] = field(default_factory=list)
    google_api_key: Optional[str] = None
    gemini_model: str = "gemini-2.5-flash"
    fallback_gemini_model: str = "gemini-3.6-flash"
    max_steps: int = 8

    def resolved_allowed_domains(self) -> List[str]:
        if self.allowed_domains:
            return list(self.allowed_domains)
        parsed = urlparse(self.controlled_page_url)
        if not parsed.hostname:
            raise ValueError("controlled_page_url must include a hostname")
        return [parsed.hostname]


@dataclass
class SessionEndpoints:
    session_id: str
    websocket_url: str
    session_viewer_url: str
    debug_url: str


@dataclass
class HumanTakeoverGate:
    session_id: str
    session_viewer_url: str
    resume_token: str = field(default_factory=lambda: uuid4().hex)
    status: HandoffStatus = HandoffStatus.READY

    def begin(self) -> None:
        self.status = HandoffStatus.WAITING

    def complete(self, token: str) -> None:
        if self.status != HandoffStatus.WAITING:
            raise RuntimeError("human takeover is not active")
        if token != self.resume_token:
            raise ValueError("invalid resume token")
        self.status = HandoffStatus.COMPLETE

    def assert_resumable(self) -> None:
        if self.status != HandoffStatus.COMPLETE:
            raise RuntimeError("automation cannot resume before explicit completion")
