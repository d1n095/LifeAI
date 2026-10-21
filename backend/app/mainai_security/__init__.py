"""Life OS security diagnostics and threat monitoring. Founder-facing, not a separate AI."""

from app.mainai_security.dashboard import build_security_dashboard, security_summary_for_ops
from app.mainai_security.leak import redact_text, sanitize_mapping, scan_text_for_leaks
from app.mainai_security.remediation import SensitiveChangeRefused
from app.mainai_security.restrictions import explain_restrictions

__all__ = [
    "SensitiveChangeRefused",
    "build_security_dashboard",
    "explain_restrictions",
    "redact_text",
    "sanitize_mapping",
    "scan_text_for_leaks",
    "security_summary_for_ops",
]
