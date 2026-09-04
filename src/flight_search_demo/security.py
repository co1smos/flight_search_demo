from __future__ import annotations

import ipaddress
from typing import Iterable
from urllib.parse import ParseResult, urlparse, urlunparse


def normalize_loopback_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    hostname = parsed.hostname
    if hostname not in {"0.0.0.0", "localhost"}:
        return raw_url
    replacement = "127.0.0.1"
    netloc = replacement
    if parsed.port is not None:
        netloc = f"{replacement}:{parsed.port}"
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth = f"{auth}:{parsed.password}"
        netloc = f"{auth}@{netloc}"
    return urlunparse(ParseResult(parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def assert_private_url(raw_url: str) -> str:
    parsed = urlparse(normalize_loopback_url(raw_url))
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("url must include a hostname")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ValueError("url host must be a literal private address or loopback") from exc
    if not (address.is_private or address.is_loopback):
        raise ValueError("url host must stay on loopback or RFC1918 space")
    return urlunparse(parsed)


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
