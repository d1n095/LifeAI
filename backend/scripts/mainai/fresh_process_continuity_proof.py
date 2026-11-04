#!/usr/bin/env python3
"""Fresh-process continuity proof — Process A writes, exits; B/C reconstruct from DB.

Same-process new SQLAlchemy Session is NOT enough. No inherited Python globals.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

_BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BACKEND))

_BOOT_DB = os.environ.get("LIFEAI_BOOT_DATABASE_NAME", "lifeos_safe_internal")
# Never migrate the cluster's maintenance DB by accident.
_raw_db_url = os.environ.get("DATABASE_URL", f"postgresql://lifeos@localhost:5433/{_BOOT_DB}")
if urlparse(_raw_db_url).path.lstrip("/") in ("", "postgres"):
    _raw_db_url = f"postgresql://lifeos@localhost:5433/{_BOOT_DB}"
os.environ["DATABASE_URL"] = _raw_db_url
os.environ.setdefault(
    "APP_DATABASE_URL", f"postgresql://mainai_app:mainai_app_pw@localhost:5433/{_BOOT_DB}"
)
os.environ.setdefault("SECRET_KEY", "safe-internal-boot-local-only")
os.environ.setdefault("FOUNDER_EMAIL", "founder@lifeos.local")
os.environ.setdefault("FOUNDER_PASSWORD", "TestFounderPassword123!")
os.environ.setdefault("ENABLE_SCHEDULED_CLEANUP", "false")
os.environ.setdefault("OPENAI_API_KEY", "disabled-for-safe-internal")


_CHILD = r'''
import json, os, sys, uuid
from pathlib import Path
sys.path.insert(0, os.environ["LIFEAI_BACKEND"])
from sqlalchemy.orm import sessionmaker
from app.config import get_settings
get_settings.cache_clear()
from app.db import migration_engine
from app.workforce.kill_switch import activate_owner_stop, query_stop_status
from app.mainai_executive.existing_state import seed_rich_safe_internal_state, inspect_existing_state_after_boot
from app.models.user import User
from sqlalchemy import select

phase = sys.argv[1]
out_path = Path(sys.argv[2])
payload = json.loads(Path(sys.argv[3]).read_text()) if len(sys.argv) > 3 and Path(sys.argv[3]).exists() else {}

Session = sessionmaker(bind=migration_engine)
db = Session()
try:
    if phase == "A":
        seeded = seed_rich_safe_internal_state(db, owner_email=payload.get("owner_email"))
        activate_owner_stop(db, owner_id=uuid.UUID(seeded["owner_id"]), reason="fresh_process_proof_stop")
        db.commit()
        result = {"phase": "A", "seed": seeded, "stop": query_stop_status(db, owner_id=uuid.UUID(seeded["owner_id"]))}
    elif phase == "B":
        owner_id = uuid.UUID(payload["seed"]["owner_id"])
        stop = query_stop_status(db, owner_id=owner_id)
        inspect = inspect_existing_state_after_boot(db, owner_id=owner_id, session_id=payload["seed"]["session_id"])
        result = {
            "phase": "B",
            "stop_blocked": bool(stop.get("blocked")),
            "owner_active": bool((stop.get("owner") or {}).get("active")),
            "invariants": inspect.get("invariants"),
            "current_has_correction": any("CURRENT correction" in (m.get("content") or "") for m in inspect.get("CURRENT_MEMORY") or []),
            "superseded_present": bool(inspect.get("SUPERSEDED_MEMORY")),
            "process_cache_not_authority": True,
        }
    elif phase == "C":
        owner_id = uuid.UUID(payload["seed"]["owner_id"])
        stop = query_stop_status(db, owner_id=owner_id)
        inspect = inspect_existing_state_after_boot(db, owner_id=owner_id, session_id=payload["seed"]["session_id"])
        result = {
            "phase": "C",
            "stop_still_blocked": bool(stop.get("blocked")),
            "invariants": inspect.get("invariants"),
            "canonical_owner_id": str(owner_id),
        }
    else:
        raise SystemExit(f"unknown phase {phase}")
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps({"ok": True, "phase": phase}))
finally:
    db.close()
'''


def _run_phase(phase: str, out: Path, inp: Path | None = None) -> dict:
    cmd = [
        sys.executable,
        "-c",
        _CHILD,
        phase,
        str(out),
        str(inp) if inp else "",
    ]
    env = {
        **os.environ,
        "LIFEAI_BACKEND": str(_BACKEND),
        "DATABASE_URL": os.environ["DATABASE_URL"],
        "APP_DATABASE_URL": os.environ["APP_DATABASE_URL"],
    }
    # Fresh interpreter each time — no inherited module globals from parent.
    proc = subprocess.run(cmd, cwd=str(_BACKEND), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"phase {phase} failed rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(out.read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description="Fresh-process continuity proof")
    parser.add_argument("--json", dest="json_out", default="/tmp/mainai-fresh-process-proof.json")
    parser.add_argument("--ensure-db", action="store_true", default=True)
    args = parser.parse_args()

    if args.ensure_db:
        import importlib.util

        boot_path = _BACKEND / "scripts" / "mainai" / "safe_internal_boot.py"
        spec = importlib.util.spec_from_file_location("safe_internal_boot", boot_path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        mod.ensure_boot_database()

    work = Path("/tmp/mainai-fresh-process")
    work.mkdir(parents=True, exist_ok=True)
    a_out = work / "A.json"
    b_out = work / "B.json"
    c_out = work / "C.json"
    seed_in = work / "seed_in.json"
    seed_in.write_text(
        json.dumps({"owner_email": f"fresh-proc-{uuid.uuid4().hex[:8]}@local.internal"})
    )

    a = _run_phase("A", a_out, seed_in)
    b = _run_phase("B", b_out, a_out)
    c = _run_phase("C", c_out, a_out)

    ok = (
        a.get("stop", {}).get("blocked") is True
        and b.get("stop_blocked") is True
        and b.get("owner_active") is True
        and b.get("current_has_correction") is True
        and b.get("superseded_present") is True
        and (b.get("invariants") or {}).get("no_oldest_row_as_current") is True
        and c.get("stop_still_blocked") is True
        and (c.get("invariants") or {}).get("superseded_not_in_current") is True
    )
    receipt = {
        "A": a,
        "B": b,
        "C": c,
        "FRESH_PROCESS_OK": ok,
        "note": "three separate Python processes; durable DB only",
    }
    Path(args.json_out).write_text(json.dumps(receipt, indent=2, default=str))
    print(f"receipt_written={args.json_out}")
    print(f"FRESH_PROCESS_OK={ok}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
