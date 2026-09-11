"""MainAI V2 -- Privacy Boundary Engine: deterministic, heuristic sanitization.

HONEST SCOPE NOTE: this is best-effort, pattern-based sanitization -- it is NOT a claim of
perfect de-identification. It exists as one defense-in-depth layer inside a pipeline whose
real guarantee comes from structure (MinimizedSignal/GeneralizedSignal cannot hold raw text
fields at all -- see types.py), not from this module catching every possible PII shape.
Any residual content this module cannot confidently classify as safe is left for the caller
to fail closed on -- this module never claims "definitely clean."
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?){2,5}\d{2,4}")
_URL_RE = re.compile(r"https?://[^\s]+")
_FILE_PATH_RE = re.compile(r"(?:/[A-Za-z0-9_.\-]+){2,}|[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]+")
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/([A-Za-z0-9_.\-]+)")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
_ACCOUNT_NUMBER_RE = re.compile(r"\b\d[\d\s-]{8,20}\d\b")
_EXACT_MONEY_RE = re.compile(
    r"\b\d+(?:[ ,]\d{3})*(?:[.,]\d{1,2})?\s?(?:kr|SEK|USD|\$|€)\b"
    r"|(?:\$|€)\s?\d+(?:[ ,]\d{3})*(?:[.,]\d{1,2})?\b",
    re.IGNORECASE,
)
# Very rough "looks like a proper name" heuristic -- two capitalized words in a row. High
# false-positive rate by design (over-redacting a non-name is far cheaper than missing one).
_NAME_LIKE_RE = re.compile(r"\b[A-ZÅÄÖ][a-zåäö]+\s[A-ZÅÄÖ][a-zåäö]+\b")
_STACK_TRACE_RE = re.compile(r'File "[^"]+", line \d+|at [\w.]+\([^)]*\)|Traceback \(most recent call last\)')


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", _EMAIL_RE),
    ("url", _URL_RE),
    ("uuid_or_session_id", _UUID_RE),
    ("file_path", _FILE_PATH_RE),
    ("stack_trace", _STACK_TRACE_RE),
    ("token_or_account_like", _TOKEN_RE),
    ("exact_money_value", _EXACT_MONEY_RE),
    ("account_number", _ACCOUNT_NUMBER_RE),
    ("phone_number", _PHONE_RE),
    ("name_like", _NAME_LIKE_RE),
)


def sanitize_text(text: str) -> tuple[str, tuple[str, ...]]:
    """Strip/redact known-shape PII from free text. Returns (sanitized_text, categories_found).

    Order matters: more specific/structural patterns (email, url, uuid, path, stack trace,
    token) run before looser ones (phone, name) so a token-shaped string inside a URL doesn't
    get double-flagged in a confusing way. home-path usernames are additionally extracted
    into their own category since a home directory path leaks the local OS username even
    after the raw path itself is redacted from a stack trace.
    """
    categories: list[str] = []
    working = text

    home_match = _HOME_PATH_RE.search(working)
    if home_match:
        categories.append("local_username")

    for category, pattern in _PATTERNS:
        if pattern.search(working):
            categories.append(category)
            working = pattern.sub(f"[REDACTED:{category}]", working)

    return working, tuple(dict.fromkeys(categories))  # de-duplicate, preserve order


# Markers that make a residual blob "known-shape enough to allow through once redacted."
# Anything that survives sanitize_text() with NO matched category but still looks like it
# could be personal (heuristic: long free text, i.e. more than a short closed-vocabulary
# token) is treated by the pipeline as an unknown blob and blocked -- see pipeline.py's
# _looks_like_unknown_personal_blob().
KNOWN_SAFE_MAX_UNMATCHED_LEN = 40
