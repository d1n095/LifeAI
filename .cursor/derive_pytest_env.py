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
_ENV_KEYS = ("DATABASE_URL", "MAINAI_APP_PASSWORD")


def read_env_keys(env_path: Path, keys: tuple[str, ...] = _ENV_KEYS) -> dict[str, str]:
    """Read specific keys from backend/.env without exporting dev-only vars into pytest."""
    wanted = set(keys)
    values: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        key = key.strip()
        if key not in wanted:
            continue
        values[key] = raw.strip().strip('"').strip("'")
    return values


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
    if len(sys.argv) == 2:
        env_path = Path(sys.argv[1])
        if not env_path.is_file():
            print(f"{env_path} is missing — run .cursor/install.sh first", file=sys.stderr)
            return 1
        vals = read_env_keys(env_path)
        database_url = vals.get("DATABASE_URL", "").strip()
        app_password = vals.get("MAINAI_APP_PASSWORD", "")
    else:
        database_url = os.environ.get("DATABASE_URL", "").strip()
        app_password = os.environ.get("MAINAI_APP_PASSWORD", "")
    test_db_name = os.environ.get("LIFEAI_TEST_DATABASE_NAME", f"lifeos_test_{os.getpid()}")
    if not database_url:
        print("DATABASE_URL must be set in backend/.env", file=sys.stderr)
        return 1
    if not app_password:
        print("MAINAI_APP_PASSWORD must be set in backend/.env", file=sys.stderr)
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
