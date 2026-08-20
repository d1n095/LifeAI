#!/usr/bin/env python3
"""Parse DATABASE_URL for Cloud Agent bootstrap (.cursor/setup-services.sh).

Prints `shlex.quote`d `LIFEOS_PASSWORD` and `LIFEOS_DB` assignments to stdout for
`eval`. Failures go to stderr without echoing the URL or password — a leaked
connection string here would be a secret in install/start logs.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from urllib.parse import unquote, urlparse

_DB_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def parse_lifeos_database_url(database_url: str) -> tuple[str, str]:
    """Return (password, database_name) for the migration superuser URL.

    DATABASE_URL must be postgresql://lifeos:<password>@.../<dbname>. The username
    is required to be `lifeos` so bootstrap never promotes `mainai_app` to
    SUPERUSER or runs migrations through the RLS runtime role.
    """
    parsed = urlparse(database_url)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ValueError("DATABASE_URL must be a postgresql:// URL")
    user = unquote(parsed.username or "")
    if user != "lifeos":
        raise ValueError(
            "DATABASE_URL username must be lifeos (migration superuser); "
            "do not point DATABASE_URL at the mainai_app runtime role"
        )
    password = unquote(parsed.password or "")
    if not password:
        raise ValueError("DATABASE_URL must include a password for the lifeos role")
    db_name = (parsed.path or "").lstrip("/")
    if not db_name or not _DB_NAME.fullmatch(db_name):
        raise ValueError("DATABASE_URL database name is missing or not a simple identifier")
    return password, db_name


def main() -> int:
    raw = os.environ.get("DATABASE_URL", "")
    if not raw.strip():
        print("DATABASE_URL must be set in backend/.env", file=sys.stderr)
        return 1
    try:
        password, db_name = parse_lifeos_database_url(raw)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(f"LIFEOS_PASSWORD={shlex.quote(password)}\n")
    sys.stdout.write(f"LIFEOS_DB={shlex.quote(db_name)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
