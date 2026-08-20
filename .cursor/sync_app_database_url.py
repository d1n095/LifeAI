#!/usr/bin/env python3
"""Keep backend/.env APP_DATABASE_URL aligned with DATABASE_URL + MAINAI_APP_PASSWORD.

setup-services.sh provisions mainai_app with MAINAI_APP_PASSWORD while the API/worker
connect through APP_DATABASE_URL — if those drift, auth fails after an otherwise successful
bootstrap. This script idempotently rewrites the APP_DATABASE_URL line in backend/.env to the
canonical derived value (same formula as ensure_app_role.py --derive-only). Never logs secrets.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

CURSOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CURSOR_DIR))

from derive_app_database_url import derive_app_database_url  # noqa: E402

_APP_DATABASE_URL_LINE = re.compile(r"^APP_DATABASE_URL=.*$", re.MULTILINE)


def sync_env_file(env_path: Path, *, database_url: str, app_password: str) -> bool:
    """Rewrite APP_DATABASE_URL in env_path. Returns True if the file changed."""
    derived = derive_app_database_url(database_url, app_password)
    text = env_path.read_text()
    new_line = f"APP_DATABASE_URL={derived}"
    if _APP_DATABASE_URL_LINE.search(text):
        updated = _APP_DATABASE_URL_LINE.sub(new_line, text, count=1)
    else:
        updated = text.rstrip("\n") + f"\n{new_line}\n"
    if updated == text:
        return False
    env_path.write_text(updated)
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: sync_app_database_url.py <backend/.env path>", file=sys.stderr)
        return 2
    env_path = Path(sys.argv[1])
    if not env_path.is_file():
        print(f"{env_path} is missing — run .cursor/install.sh first", file=sys.stderr)
        return 1

    database_url = os.environ.get("DATABASE_URL", "").strip()
    app_password = os.environ.get("MAINAI_APP_PASSWORD", "")
    if not database_url:
        print("DATABASE_URL must be set in backend/.env", file=sys.stderr)
        return 1
    if not app_password:
        print("MAINAI_APP_PASSWORD must be set in backend/.env", file=sys.stderr)
        return 1

    try:
        changed = sync_env_file(env_path, database_url=database_url, app_password=app_password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if changed:
        print("--> Synced APP_DATABASE_URL from DATABASE_URL + MAINAI_APP_PASSWORD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
