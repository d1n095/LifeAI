"""Redact secrets in logs, diffs, config and API responses without copying raw values."""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Patterns detect likely secrets. Matches are masked immediately; the raw value is discarded.
_SECRET_SPECS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_sk", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_pat", re.compile(r"ghp_[A-Za-z0-9]{36,}")),
    ("github_fine_grained", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("postgres_url", re.compile(r"postgres(?:ql)?://[^:\s/]+:[^@\s/]+@")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("assignment", re.compile(r"(?i)(?:api[_-]?key|secret_key|password|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}")),
)

_REDACTED_KEYS = {
    "password",
    "secret",
    "secret_key",
    "api_key",
    "token",
    "access_token",
    "refresh_token",
    "github_token",
    "authorization",
    "cookie",
    "private_key",
    "founder_password",
    "smtp_password",
}


def mask_secret(raw: str, *, kind: str = "secret") -> str:
    """Structural mask. Never echo the original secret, not even a suffix."""
    if kind == "pem_private_key":
        return "-----BEGIN PRIVATE KEY-----****"
    if raw.startswith("sk-"):
        return "sk-****"
    if raw.startswith("AKIA"):
        return "AKIA****"
    if raw.startswith("ghp_"):
        return "ghp_****"
    if raw.startswith("github_pat_"):
        return "github_pat_****"
    if raw.startswith("xox"):
        return "xox*-****"
    if raw.lower().startswith("bearer "):
        return "Bearer ****"
    if raw.startswith("postgres"):
        return "postgres://****:****@"
    if raw.startswith("eyJ"):
        return "eyJ****"
    return f"{kind}****"


def redact_text(value: str | None) -> tuple[str, list[str]]:
    """Return (redacted_text, categories). The original match is never kept."""
    if not value:
        return "", []
    categories: list[str] = []
    redacted = value
    for kind, pattern in _SECRET_SPECS:
        def _replace(match: re.Match[str], *, _kind: str = kind) -> str:
            categories.append(_kind)
            return mask_secret(match.group(0), kind=_kind)

        redacted = pattern.sub(_replace, redacted)
    return redacted, categories


def scan_text_for_leaks(value: str | None, *, source: str) -> list[dict[str, Any]]:
    """Findings for leak-like material. Evidence is the redacted span only."""
    if not value:
        return []
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for kind, pattern in _SECRET_SPECS:
        for match in pattern.finditer(value):
            masked = mask_secret(match.group(0), kind=kind)
            key = (kind, source)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                {
                    "id": _stable_id("leak", kind, source),
                    "kind": "leaked_secret",
                    "category": kind,
                    "severity": "high" if kind != "assignment" else "medium",
                    "confidence": "confirmed",
                    "source": source,
                    "evidence": {"masked": masked, "raw_copied": False},
                    "component": source,
                    "recommended_action": "Rotate the credential and remove it from the durable store. Do not paste the raw value into chat or logs.",
                    "status": "open",
                }
            )
    return findings


def sanitize_mapping(payload: Any) -> Any:
    """Recursively mask secret-bearing keys and redact string values."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if str(key).lower() in _REDACTED_KEYS:
                out[key] = "****"
            else:
                out[key] = sanitize_mapping(value)
        return out
    if isinstance(payload, list):
        return [sanitize_mapping(item) for item in payload]
    if isinstance(payload, str):
        redacted, _ = redact_text(payload)
        return redacted
    return payload


def mask_identity(value: str | None) -> str | None:
    if not value:
        return None
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}****@{domain}"
    if len(value) <= 2:
        return "****"
    return f"{value[:1]}****"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _stable_id(*parts: str) -> str:
    return "sec_" + fingerprint("|".join(parts))
