"""Best-effort redaction of secret-looking material.

This runs before anything leaves the process: before a Jev request, before a
SQLite write, and before the UI renders an event.

Limitation, stated plainly: this is a pattern matcher. It recognises the shapes
listed below and nothing else. It does not guarantee that every secret in an
arbitrary provider error string is detected, and it must not be described as if
it did. Run it against synthetic events by default.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .models import SyncFailureEvent

#: (label, compiled pattern, replacement builder). Order matters: the most
#: specific shapes run first so a broad rule cannot eat a narrower one.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----", re.S)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    (
        "authorization_header",
        re.compile(
            r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*"
            r"(?:(?:bearer|basic|digest|token)\s+)?\S+"
        ),
    ),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]{8,}=*")),
    ("basic_auth_header", re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{12,}=*")),
    ("api_key_header", re.compile(r"(?i)\b(x-api-key|x-auth-token|api-key)\s*[:=]\s*\S+")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("url_credentials", re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s:/@]+:[^\s:/@]+@")),
    (
        "sensitive_parameter",
        re.compile(
            r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|"
            r"api[_-]?key|apikey|auth[_-]?token|session[_-]?token|password|passwd|pwd|"
            r"secret|signature|sig|credential)\b\s*[:=]\s*[\"']?[^\s\"'&,;)]{4,}[\"']?"
        ),
    ),
    # A long opaque token that mixes cases and digits. Guarded so ordinary
    # prose, hostnames and UUIDs are not swallowed.
    (
        "opaque_token",
        re.compile(
            r"\b(?=[A-Za-z0-9_\-]{28,})(?=[A-Za-z0-9_\-]*[a-z])(?=[A-Za-z0-9_\-]*[A-Z])"
            r"(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{28,}\b"
        ),
    ),
]

#: Fields on the event that carry free text a provider may have written into.
_TEXT_FIELDS = ("error_message", "operation", "failure_stage", "provider_error_code", "retry_after")

REDACTION_NOTICE = (
    "Redaction is pattern-based and best effort. It covers the documented secret "
    "shapes only and cannot guarantee that every secret is detected."
)


def redact_text(text: str | None) -> tuple[str | None, list[str]]:
    """Redact known secret shapes in ``text``.

    Returns the redacted text and the sorted labels of the patterns that fired.
    """
    if not text:
        return text, []
    found: set[str] = set()
    out = text
    for label, pattern in _PATTERNS:
        def _sub(match: re.Match[str], _label: str = label) -> str:
            found.add(_label)
            # Preserve the key name so "password=<x>" stays readable as a fact.
            if _label in {"sensitive_parameter", "authorization_header", "api_key_header"}:
                key = match.group(1)
                return f"{key}=[REDACTED:{_label}]"
            if _label == "url_credentials":
                return f"{match.group(1)}[REDACTED:{_label}]@"
            return f"[REDACTED:{_label}]"

        out = pattern.sub(_sub, out)
    return out, sorted(found)


def sanitize_event(event: SyncFailureEvent) -> tuple[SyncFailureEvent, list[str]]:
    """Return a redacted copy of ``event`` plus the labels that fired."""
    updates: dict[str, Any] = {}
    labels: set[str] = set()
    for field in _TEXT_FIELDS:
        value = getattr(event, field, None)
        if isinstance(value, str):
            cleaned, found = redact_text(value)
            if found:
                updates[field] = cleaned
                labels.update(found)
    if not updates:
        return event, []
    return event.model_copy(update=updates), sorted(labels)


def sanitize_mapping(payload: Any) -> tuple[Any, list[str]]:
    """Recursively redact strings inside an arbitrary JSON-like structure."""
    labels: set[str] = set()

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            cleaned, found = redact_text(node)
            labels.update(found)
            return cleaned
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [_walk(v) for v in node]
        return node

    return _walk(payload), sorted(labels)


def describe_redactions(labels: Iterable[str]) -> str:
    labels = list(labels)
    if not labels:
        return "No secret-shaped values matched."
    return "Redacted: " + ", ".join(labels)
