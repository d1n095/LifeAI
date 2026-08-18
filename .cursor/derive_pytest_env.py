#!/usr/bin/env python3
"""Derive pytest DATABASE_URL / APP_DATABASE_URL for Cloud Agent backend tests.

tests/conftest.py defaults to postgresql://lifeos@localhost:5433/lifeos_test_<pid> with
APP role password mainai_app_pw. Cloud Agent VMs run system Postgres on :5432 with password
auth and MAINAI_APP_PASSWORD from backend/.env — pytest fails unless the caller exports
matching URLs first. This script builds disposable test DB URLs on the same host/credentials
as DATABASE_URL without touching the dev `lifeos` database.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

CURSOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CURSOR_DIR))

from derive_app_database_url import derive_app_database_url  # noqa: E402
from parse_database_url import parse_lifeos_database_url  # noqa: E402

_DB_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def derive_pytest_database_urls(database_url: str, app_password: str, test_db_name: str) -> tuple[str, str]:
    """Return (DATABASE_URL, APP_DATABASE_URL) for pytest on the Cloud Agent Postgres."""
    parse_lifeos_database_url(database_url)  # validates lifeos superuser URL
    if not _DB_NAME.fullmatch(test_db_name):
        raise ValueError("test database name must be a simple identifier")
    parts = urlsplit(database_url)
    admin_url = urlunsplit((parts.scheme, parts.netloc, f"/{test_db_name}", "", ""))
    app_url = derive_app_database_url(admin_url, app_password)
    return admin_url, app_url


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    app_password = os.environ.get("MAINAI_APP_PASSWORD", "")
    test_db_name = os.environ.get("LIFEAI_TEST_DATABASE_NAME", f"lifeos_test_{os.getpid()}")
    if not database_url:
        print("DATABASE_URL must be set (source backend/.env first)", file=sys.stderr)
        return 1
    if not app_password:
        print("MAINAI_APP_PASSWORD must be set (source backend/.env first)", file=sys.stderr)
        return 1
    try:
        admin_url, app_url = derive_pytest_database_urls(database_url, app_password, test_db_name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(f"DATABASE_URL={shlex.quote(admin_url)}\n")
    sys.stdout.write(f"APP_DATABASE_URL={shlex.quote(app_url)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
