#!/usr/bin/env python3
"""Boot MainAI in SAFE INTERNAL mode from the current checkout.

Usage (from backend/, against local test/ops DB — never prod by default):

  unset DATABASE_URL APP_DATABASE_URL   # uses app settings / localhost defaults
  # or point explicitly:
  # export DATABASE_URL=postgresql://lifeos@localhost:5433/lifeos_test

  python scripts/mainai/safe_internal_boot.py
  python scripts/mainai/safe_internal_boot.py --json /tmp/mainai-boot-receipt.json

Verifies: start → meaningful task → durable receipts → shutdown → restart → resume
Provider call count must remain 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="MainAI safe-internal boot")
    parser.add_argument("--owner-email", default=None)
    parser.add_argument("--task", default=None, help="Override founder task text")
    parser.add_argument("--json", dest="json_out", default=None, help="Write receipt JSON")
    parser.add_argument("--session-id", default=None)
    args = parser.parse_args()

    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine
    from app.mainai_executive.internal_start import (
        DEFAULT_FOUNDER_TASK,
        run_first_real_internal_boot,
    )
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)
    Session = sessionmaker(bind=migration_engine)
    db = Session()
    try:
        report = run_first_real_internal_boot(
            db,
            owner_email=args.owner_email,
            founder_task=args.task or DEFAULT_FOUNDER_TASK,
            session_id=args.session_id,
        )
        payload = report.as_dict()
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str))
            print(f"receipt_written={args.json_out}")
        # Concise operator summary
        print("=== MAINAI SAFE-INTERNAL BOOT ===")
        print(f"readiness={report.readiness_level}")
        print(f"session_id={report.session_id}")
        print(f"first_task_ok={report.first_task_ok} phase={report.first_task_phase}")
        print(f"local_attempt_used={report.local_attempt_used} school_used={report.school_used}")
        print(f"provider_call_count={report.provider_call_count}")
        print(f"shutdown_ok={report.shutdown_ok} restart_ok={report.restart_ok} resume_ok={report.resume_ok}")
        print(f"offline_ok={report.offline_ok}")
        print(f"PROVIDER_ENABLED={report.status_surface.get('PROVIDER_ENABLED')}")
        ok = (
            report.first_task_ok
            and report.shutdown_ok
            and report.restart_ok
            and report.resume_ok
            and report.provider_call_count == 0
            and report.local_attempt_used
            and report.school_used
        )
        print(f"BOOT_SUCCESS={ok}")
        return 0 if ok else 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
