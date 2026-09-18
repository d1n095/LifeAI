from __future__ import annotations

import argparse
import json
import os
import signal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.mainai_founder_boot.service import FounderBootError, boot_mainai_founder_only
from app.models.user import User, UserRole
from app.request_context import current_user_id as current_user_id_var


def run_founder_boot_entrypoint(
    db: Session,
    *,
    founder_email: str,
    founder_request: str | None = None,
    create_safe_program: bool = False,
) -> dict[str, Any]:
    """Run one local founder-only boot from durable database state.

    This is the minimal real startup seam for the first local boot.  It deliberately does not
    talk to real model/provider services and does not grant any additional authority; it only
    binds an existing active founder row, creates durable boot/audit/status rows, and returns
    the Founder Brief produced from those rows.
    """
    founder = db.scalar(select(User).where(User.email == founder_email, User.role == UserRole.founder, User.is_active.is_(True)))
    if founder is None:
        raise FounderBootError("active founder not found")
    founder_id = founder.id
    token = current_user_id_var.set(str(founder_id))
    try:
        # The founder lookup above may have opened a transaction before the RLS contextvar
        # was set.  Start a fresh transaction so SessionLocal.after_begin binds
        # app.current_user_id before inserting owner-scoped boot/covenant rows.
        db.rollback()
        founder = db.get(User, founder_id, populate_existing=True)
        if founder is None:
            raise FounderBootError("active founder not found")
        kill_after = os.environ.get("MAINAI_BOOT_KILL_AFTER_EVENT")

        def _kill_hook(hook_db: Session, _boot, event_type: str) -> None:
            if kill_after and event_type == kill_after:
                hook_db.commit()
                os.kill(os.getpid(), signal.SIGKILL)

        result = boot_mainai_founder_only(
            db,
            founder=founder,
            founder_request=founder_request,
            create_safe_program=create_safe_program,
            event_hook=_kill_hook if kill_after else None,
        )
        db.commit()
        return result.founder_brief
    except Exception:
        db.rollback()
        raise
    finally:
        current_user_id_var.reset(token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local founder-only MainAI boot candidate.")
    parser.add_argument("--founder-email", required=True)
    parser.add_argument("--request", default=None)
    parser.add_argument("--create-safe-program", action="store_true")
    args = parser.parse_args(argv)
    db = SessionLocal()
    try:
        brief = run_founder_boot_entrypoint(
            db,
            founder_email=args.founder_email,
            founder_request=args.request,
            create_safe_program=args.create_safe_program,
        )
    finally:
        db.close()
    print(json.dumps(brief, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
