#!/usr/bin/env python3
"""Derive APP_DATABASE_URL from DATABASE_URL + MAINAI_APP_PASSWORD (stdlib only).

Mirrors backend/scripts/security/ensure_app_role.py::_derive_app_database_url so Cloud
Agent bootstrap stays aligned with production/worker derivation without importing psycopg2.
"""

from __future__ import annotations

import sys
from urllib.parse import quote, urlsplit, urlunsplit

APP_ROLE = "mainai_app"


def _app_username(admin_username: str) -> str:
    if "." in admin_username:
        _, _, tenant = admin_username.partition(".")
        return f"{APP_ROLE}.{tenant}"
    return APP_ROLE


def derive_app_database_url(database_url: str, app_password: str) -> str:
    """Pure string derivation — byte-identical to ensure_app_role._derive_app_database_url."""
    parts = urlsplit(database_url)
    if not parts.hostname or not parts.username:
        raise ValueError("DATABASE_URL must include a hostname and username")
    if not app_password:
        raise ValueError("MAINAI_APP_PASSWORD must not be empty")
    app_username = _app_username(parts.username)
    app_netloc = (
        f"{quote(app_username, safe='.')}:{quote(app_password, safe='')}"
        f"@{parts.hostname}:{parts.port or 5432}"
    )
    return urlunsplit((parts.scheme, app_netloc, parts.path, "", ""))


def main() -> int:
    import os
    import shlex

    database_url = os.environ.get("DATABASE_URL", "").strip()
    app_password = os.environ.get("MAINAI_APP_PASSWORD", "")
    if not database_url:
        print("DATABASE_URL must be set in backend/.env", file=sys.stderr)
        return 1
    if not app_password:
        print("MAINAI_APP_PASSWORD must be set in backend/.env", file=sys.stderr)
        return 1
    try:
        derived = derive_app_database_url(database_url, app_password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(f"APP_DATABASE_URL={shlex.quote(derived)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
